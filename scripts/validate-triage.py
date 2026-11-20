"""Check one accession's msdial-unit-triage files (v1 or v2) and their decision records before anything is planned.

A triage file (schemas/msdial-unit-triage.v2.json, written by the msdial-unit-triage skill; v1 files stay readable)
records which Catalog rows and archive members one analysis unit keeps. Every choice in it is a model inference. This
script is the deterministic stage that stands between those inferences and a plan: it reads the triage files of one
accession as a set, with the decision record (msdial-llm-decision-record.v1) beside each and the accession's
review-log.jsonl, and returns pass, hold or error (docs/llm-assisted-curation.md sections 2.2 to 2.4).

    python scripts/validate-triage.py <triage dir>
        [--catalog <handoffs dir | catalog.json>]   Catalog rows (and handoff sha256) to check coverage against
        [--archive-listing <tsv>]...                 zip member listing(s): path, size, crc32 [, archive]
        [--ground-truth <ground_truth.json>]         score the assignment against a known answer (eval)
        [--code-root <dir>]                          the released code (skill, prompt templates) at its commit,
                                                     read in place of git at each record's repo_commit
        [--release-bundle <dir>]                     the release bundle the triage dir sits in (found on its own
                                                     when <dir>/release-index.json is two levels up)
        [--emit-selection]                           add each unit's selection to the report and append a
                                                     validator_verdict per decision to the review log
        [--payload-only]                             the payloads alone: no record, transcript, context or revision
                                                     check (the in-session spec check)
        [--polarity-split <child payload>]...        a polarity-split child (the author's decision of 2026-11-19),
                                                     checked in place of its parent's payload (POLARITY SPLIT below)
        [--json]

The triage directory holds one <unit_id>.json per unit, all of one accession (in a campaign,
<campaign dir>/triage/<accession>/). A unit redone is kept as a revision, <unit_id>.r<N>.json, and the highest
N is the one checked (it supersedes <unit_id>.json and every earlier revision); a decision record beside a
triage (<name>.record.json, the envelope naming the model and prompt) is not a triage.
--catalog takes either the campaign's handoffs directory
(<unit_id>/handoff.json and samples.json, as msdial_catalog_reanalysis_handoff writes them; it may span many
accessions, and only the units of the triaged accession are checked) or a catalog.json with
{"accession", "units": [{"unit_id", "declared", "rows": [{"sample_id", "raw_file"}]}]}, the shape the eval
fixtures use. An archive listing is the archive-members TSV the download store keeps, or any TSV with path,
size and crc32 columns; it may be read remotely from the zip central directory before download.

A check that cannot run is not passed: without --catalog every unit holds (rows not checked against the
handoff), and without --archive-listing every unit holds (byte-identical content not ruled out).

A kept row may name its file by member_path or only by raw_file. Both resolve to one key (the listing, or
without one the member paths the triage files name, by exact name, then stem), so the same file is the
same file in every check, and its name tokens and format are read from the member it resolves to.

THE DECISION RECORD (section 2.3). Beside each payload the wrapper writes <name>.record.json. This script recomputes,
refusing any mismatch (T-RECORD): decision_id and payload.sha256 from the payload's bytes; the sha256 and bytes of
every inputs[] item, read relative to the record; skill.sha256 from SKILL.md at repo_commit (git show in this
repository, or --code-root), and the skill's version; prompt.template_sha256 at repo_commit and
prompt.rendered_sha256; wrapper.sha256 at its repo_commit; every tool_calls[].result_sha256; transcript.sha256 of the
released redacted transcript. It refuses a tool call that is not on the skill's allowed-tools list (a call the harness
did not offer and refused with "No such tool available" never ran: a note, not an error), and a tool_calls
list that differs from the calls the released transcript shows in order, server-qualified name, arguments or stored
result (T-TOOLS); a model_id or session_id the transcript does not name (T-RECORD); and a payload whose
catalog_handoff_sha256 is not the catalog_handoff input its record binds (T-HANDOFF). A context that is not clean
(configuration, context_files, memory, hooks, an MCP server the skill does not allow or one the calls use unlisted,
empty harness_flags, a claude-code session without the clean flags) is a hold (T-CONTEXT). It fails closed: a check
that cannot run holds (the skill, the template or the wrapper not readable at repo_commit, a wrapper that was not a
commit's, a v2 payload without a record, a record without a released transcript (T-TRANSCRIPT), a model 'unknown', a
tool call without a stored result, a record that binds no Catalog evidence or no archive listing, a listing the record
binds that was not given or one given that no record binds). A v1 payload may have no record; it is reported
rationale_not_recorded (a note, which changes no verdict).

THE DERIVATION (section 2.2). A payload the wrapper expanded from the agent's spec (<accession>.spec.json, schemas/
msdial-unit-triage-spec.v1.json) carries "derivation", and so does its record: the spec's path, sha256 and bytes, the
expander (scripts/expand-triage-spec.py) and the digest script (scripts/triage-digest.py) by version, sha256 and commit,
and the context (decided_at, model, agent, skill_version, purpose); each script binds in `tree` the sha256 of the files
it reads beside itself (SPEC_CODE_TREE), and every record's hashes are checked against the bytes read (a mismatch is an
error, unreadable code a hold). T-SPEC re-runs that expander at its commit (in this process when the commit's bytes are
the running tree's, else from a scratch copy of the commit's files) over the inputs the record binds, and refuses any
byte of difference, any X-* finding, a context that is not the record's, a payload with a derivation whose record binds
none, a v2 payload without a derivation whose record names skill 1.2.0 or later or whose accession has a spec, a session
that wrote its unit's payload itself, and a revision spec that changes a unit it does not revise (X-SCOPE, re-checked).
T-DIGEST rebuilds inputs/digest.md from the same inputs with the digest script at its commit. Every other check runs on
the expanded payload as on any other. A spec file is never read as a payload.

THE REVIEW LOG (section 2.4). For a v2 payload, a ready unit that carries a blocking flag passes only when
review-log.jsonl holds a person_answer whose question_code is the flag's code and whose decision_id is this payload or
one it supersedes (by its record's supersedes, or a revision entry); v1 payloads keep their person_answers. A revision
that drops a blocking flag the payload it supersedes carried passes only when the cause of its revision entry cites
such a person_answer (T-REVISION). A review log whose hash chain is broken is an error (T-LOG).

RELEASE-BUNDLE MODE (sections 3 and 5.3). In a release bundle a triage input that held an absolute path (a Catalog
handoff) is released as a projection: release-index.json's triage_inputs names its original_sha256 and the
release_sha256 of the released bytes. Such a file is checked through the index: its bytes must hash to
release_sha256, and original_sha256 must be the sha256 the record (or, for --catalog, the triage) names.

Levels:
- error: the triage file breaks the contract (schema, coverage, a file in two units, byte-identical content
  settled by name, polarity or chromatography that disagrees with its unit or child, an unflagged MS1-only or
  mixed acquisition, a kept mzXML or mzData file, one acquisition kept twice in two formats, a member both
  kept and excluded, an exclusion reason the listing or the unit's own status and flags contradict, evidence or a
  decision-record input that cites the Class proposal (T-CLASS: the triage runs before the Class and never binds it,
  WS0 decision F), a decision record that does not hash to what it names, a tool the skill does not allow, ...).
  The triage must be redone; nothing may be planned from it.
- hold: the file is valid but something waits for a person: a unit that needs a person, abstained or is out
  of scope, flagged shared content, a member nobody accounted for, a kept file inferred at low confidence in a ready
  unit without a person's inferred_low_confidence answer (T-CONFIDENCE), a context that was not clean, a check that
  could not run.
- pass: every unit is ready, or excluded by the triage policy of 2026-11-18 (status excluded: it keeps, asks and
  holds nothing, and the plan leaves it out as triage_unit_excluded), and nothing waits.
- note: recorded, never a verdict (rationale_not_recorded).

Exit status: 0 pass, 1 hold, 2 error. The script reads only, except that --emit-selection appends validator_verdict
entries to the accession's review-log.jsonl (through scripts/campaign/curated.append_review_log); a release bundle is
never written to.
"""

from __future__ import annotations

import argparse
import atexit
import copy
import csv
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_ID = "msdial-unit-triage.v1"
SCHEMA_V2_ID = "msdial-unit-triage.v2"
SCHEMA_PATH = REPO_ROOT / "schemas" / "msdial-unit-triage.v1.json"
SCHEMA_PATHS = {SCHEMA_ID: SCHEMA_PATH, SCHEMA_V2_ID: REPO_ROOT / "schemas" / "msdial-unit-triage.v2.json"}
RECORD_SCHEMA_ID = "msdial-llm-decision-record.v1"
RECORD_SCHEMA_PATH = REPO_ROOT / "schemas" / "msdial-llm-decision-record.v1.json"
SPEC_SCHEMA_ID = "msdial-unit-triage-spec.v1"
EXPANDER_PATH = "scripts/expand-triage-spec.py"
DIGEST_SCRIPT_PATH = "scripts/triage-digest.py"
# What the expander and the digest script read beside themselves, at their commit (T-SPEC, T-DIGEST).
SPEC_CODE_TREE = ("scripts/validate-triage.py", "schemas/msdial-unit-triage.v1.json", "schemas/msdial-unit-triage.v2.json",
                  "schemas/msdial-unit-triage-spec.v1.json")
SKILL_NAME = "msdial-unit-triage"
REVIEW_LOG = "review-log.jsonl"
RELEASE_INDEX = "release-index.json"
TOOL_NAME = "validate-triage.py"

RAW_EXTENSIONS = (".raw", ".mzml", ".mzxml", ".mzdata", ".wiff", ".wiff2", ".d", ".lcd", ".cdf", ".abf", ".ibf", ".baf", ".tdf")
VENDOR_FOLDER = re.compile(r"(?i)^(.*?\.(?:d|raw))/.+")  # Agilent/Bruker .d and Waters .raw directories
DECLARED_SOURCES = {"catalog_row", "catalog_unit", "mwtab", "repository_page"}
WEAK_FIELDS = {"size", "size_bytes", "modified", "mtime", "timestamp", "crc32"}
MIN_DUPLICATE_BYTES = 1024  # AppleDouble stubs and empty files share CRCs without meaning anything

# Flags that keep a unit from being ready until a person has answered them.
BLOCKING_FLAGS = {
    "shared_content",
    "folder_header_disagreement",
    "polarity_switching",
    "ms1_only",
    "mixed_acquisition",
    "name_token_disputed",
}
# Flags that only inform a report and never hold a unit (decisions of 2026-11-18: inclusion- or exclusion-list DDA is
# untargeted DDA and in scope, flagged targeted_dda so a report can name the MS2-selection bias; byte-identical copies
# found only among excluded members are duplicate_copies_excluded, never shared_content; and the triage policy P1 to
# P5 of docs/llm-assisted-curation.md section 2.2: MS1-only files left out are ms1_only_excluded, an MS1-only reading
# too weak to leave a file out is ms1_only_suspected, m/z-segmented DDA is segmented_acquisition, and file evidence
# that contradicts the Catalog's declared technical fields wins and is recorded as declared_technical_disputed).
INFORMATIONAL_FLAGS = {
    "targeted_dda",
    "duplicate_copies_excluded",
    "catalog_defect",
    "target_omics_doubt",
    "segmented_acquisition",
    "declared_technical_disputed",
    "ms1_only_excluded",
    "ms1_only_suspected",
}
# The skill version from which the triage policy of 2026-11-18 holds. A payload an earlier skill wrote is judged by the
# rules its agent was given: declared_technical_disputed blocks, and mixed acquisition modes are judged over all kept
# files, children or not (LEGACY_BLOCKING_FLAGS, legacy_policy).
POLICY_2026_11_18_SKILL = (1, 3, 0)
LEGACY_BLOCKING_FLAGS = {"declared_technical_disputed"}


def legacy_policy(payload: dict[str, Any] | None) -> bool:
    """Whether a payload was written by a skill before 1.3.0 (decided_by.skill_version), which the 2026-11-18 policy
    does not reach back to."""
    version = str(((payload or {}).get("decided_by") or {}).get("skill_version") or "")
    try:
        return tuple(int(part) for part in version.split(".")) < POLICY_2026_11_18_SKILL
    except ValueError:
        return False


def blocking_flags_for(payload: dict[str, Any] | None) -> set[str]:
    """The flags that keep this payload's unit from being ready until a person answers them."""
    return BLOCKING_FLAGS | (LEGACY_BLOCKING_FLAGS if legacy_policy(payload) else set())


# Why a unit is excluded by the triage policy of 2026-11-18 (status excluded): no file with product-ion spectra is left
# once MS1-only files are left out (P1), the deposit holds no raw file of the unit, or the unit's arm's copies are
# byte-identical to another arm's and no post-download reassignment exists yet (P5). Such a unit asks nothing and holds
# nothing: the plan leaves it out (curated.TRIAGE_UNIT_EXCLUDED) with this reason.
UNIT_EXCLUSION_REASONS = ("no_product_ion_files", "no_files", "shared_content_unassigned")
# The question code of a person's answer that lets a ready unit rest on a kept file inferred at low confidence
# (T-CONFIDENCE): the person looked at it (scripts/record-person-answer.py, section 2.2).
LOW_CONFIDENCE_CODE = "inferred_low_confidence"
# What a claude-code session must have run with for its context to count as clean (scripts/run-triage-agent.py
# CLEAN_FLAGS and its environment and settings, as agent.context.harness_flags lists them).
CLAUDE_CODE_CLEAN_FLAGS = (
    "--restricted", "--strict-mcp-config", "--tools", "--allowedTools", "--permission-mode", "dontAsk",
    "--disable-slash-commands", "--no-session-persistence", "env CLAUDE_CODE_DISABLE_CLAUDE_MDS=1",
    "env CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", 'settings {"disableAllHooks":true}',
)
# The roles a unit triage's record binds its Catalog evidence under.
CATALOG_ROLES = ("catalog_handoff", "catalog_samples", "catalog_unit")


# ------------------------------------------------------------------------------------------ polarity split
# POLARITY SPLIT (the author's decision of 2026-11-19): "Polarity switching units are analysed: for each file run a POS
# process and a NEG process separately." MS-DIAL 5 LC-MS reads only the scans whose polarity matches the project's ion
# mode, so the files of a unit acquired in polarity switching are run twice, once positive and once negative. A unit is
# split only where the decision is all that holds it (polarity_split_refusal): status needs_person, every question
# polarity_switching, no other blocking flag, no children, its unit_technical ion mode switching, and every row it left
# out for the question naming its archive member. Each child is derived from the parent payload by
# derive_polarity_split alone, deterministically: the rows left out for polarity switching are kept (each file's own
# ion mode stays switching), status ready, needs_person empty, unit_technical.ion_mode the child's, a polarity_split
# object {ion_mode, reason, parent_unit_id, sibling_ion_mode} (the contract Interactive reads), and derived_from (the
# parent payload's path and sha256, its decision record's sha256, and the decision). A child is validated with
# --polarity-split beside its parent's accession: the parent's file, record, derivation and revision are checked as
# they are, the child must be exactly what derive_polarity_split makes of that parent (T-SPLIT), and every content
# check then runs on the child, the decision standing as the answer to polarity_switching.
#
# OWN POLARITY (the author's decision of 2026-11-20). A repository may register a positive and a negative analysis over
# one set of polarity-switching files (Metabolomics Workbench: two Catalog units, two unit ids, the same files). Such a
# unit is not split in two: it runs once, at its own polarity, and its partner runs the other. A unit held as above
# reaches this rule (polarity_own_refusal) where its unit ion mode is positive or negative, or where it is switching
# and a partner holds the same files at the opposite polarity (its own polarity then its declared ion mode). Its
# partner (polarity_partner) is the one other unit of the accession that leaves out for polarity switching exactly the
# archive members it does, at the opposite polarity (its unit ion mode, else its declared one); with none it is null.
# The derivation is derive_polarity_split at the unit's own polarity with polarity_split.partner_unit_id added (the
# field Interactive 0.5.71 reads) and the decision of 2026-11-20 in derived_from. T-UNIQ lets two units keep the same
# files only where both are such children of opposite polarities naming each other.
POLARITY_SPLIT_REASON = "polarity_switching"
POLARITY_SPLIT_MODES = ("positive", "negative")
POLARITY_SPLIT_DECISION = {
    "date": "2026-11-19",
    "decided_by": "author",
    "reason": POLARITY_SPLIT_REASON,
    "statement": "Polarity switching units are analysed: for each file run a POS process and a NEG process separately.",
}
POLARITY_SPLIT_KEYS = ("ion_mode", "reason", "parent_unit_id", "sibling_ion_mode")
POLARITY_OWN_DECISION = {
    "date": "2026-11-20",
    "decided_by": "author",
    "reason": POLARITY_SPLIT_REASON,
    # The author's words in chat, verbatim, answering the recommendation to run each unit at its own polarity.
    "statement": "13単位は推奨でお願いします (accepted recommendation: a polarity-switching unit registered at one "
                 "polarity, beside a unit of the other polarity over the same files, is run once at its own polarity)",
}
POLARITY_OWN_KEYS = (*POLARITY_SPLIT_KEYS, "partner_unit_id")
DERIVED_FROM_KEYS = ("kind", "triage_relative_path", "triage_sha256", "triage_record_sha256", "decision")


def _split_question_rows(payload: Mapping[str, Any]) -> set[int]:
    rows: set[int] = set()
    for question in payload.get("needs_person") or []:
        if isinstance(question, Mapping) and question.get("code") == POLARITY_SPLIT_REASON:
            rows |= {int(i) for i in question.get("row_indices") or [] if isinstance(i, int)}
    return rows


def polarity_split_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The rows a payload left out for polarity switching: reason polarity_switching, or needs_person on a row a
    polarity_switching question names."""
    asked = _split_question_rows(payload)
    return [dict(item) for item in payload.get("excluded_rows") or [] if isinstance(item, Mapping) and (
        item.get("reason") == POLARITY_SPLIT_REASON or (item.get("reason") == "needs_person" and item.get("row_index") in asked))]


def _polarity_hold_refusal(payload: Mapping[str, Any] | None) -> str:
    """Why a payload is not held on polarity switching alone (every condition of the split but the unit ion mode), or
    ""."""
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA_V2_ID:
        return f"not a {SCHEMA_V2_ID} payload"
    if payload.get("polarity_split") is not None:
        return "a polarity-split child is not split again"
    if payload.get("status") != "needs_person":
        return f"status {payload.get('status')}, not needs_person"
    codes = {str(q.get("code")) for q in payload.get("needs_person") or [] if isinstance(q, Mapping)}
    if codes != {POLARITY_SPLIT_REASON}:
        return "its questions are " + (", ".join(sorted(codes)) or "none") + f", not {POLARITY_SPLIT_REASON} alone"
    if payload.get("children"):
        return "a unit with children is not split by polarity"
    flags = {str(f.get("code")) for f in payload.get("flags") or [] if isinstance(f, Mapping)}
    if POLARITY_SPLIT_REASON not in flags:
        return f"no {POLARITY_SPLIT_REASON} flag"
    others = sorted((flags & blocking_flags_for(dict(payload))) - {POLARITY_SPLIT_REASON})
    if others:
        return "other blocking flags: " + ", ".join(others)
    taken = polarity_split_rows(payload)
    if not taken:
        return "no row is left out for polarity switching"
    unresolved = [item.get("row_index") for item in taken if not item.get("member_path")]
    if unresolved:
        return f"{len(unresolved)} row(s) left out for polarity switching name no archive member"
    indices = {item.get("row_index") for item in taken}
    held = [item for item in payload.get("excluded_rows") or [] if isinstance(item, Mapping)
            and item.get("reason") == "needs_person" and item.get("row_index") not in indices]
    if held:
        return f"{len(held)} row(s) are held for a person on another question"
    members = [item for item in payload.get("excluded_members") or [] if isinstance(item, Mapping)
               and item.get("reason") in (POLARITY_SPLIT_REASON, "needs_person")]
    if members:
        return f"{len(members)} archive member(s) are left out for polarity switching or a person"
    return ""


def _unit_polarity(payload: Mapping[str, Any]) -> str | None:
    """A unit's own polarity: its unit ion mode where positive or negative, else its declared one, else None."""
    technical = payload.get("unit_technical") or {}
    for value in (technical.get("ion_mode"), (technical.get("declared") or {}).get("ion_mode")):
        ion = normalise_ion(value)
        if ion in POLARITY_SPLIT_MODES:
            return ion
    return None


def _split_members(payload: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(str(item.get("member_path")) for item in polarity_split_rows(payload) if item.get("member_path"))


def polarity_partner(payload: Mapping[str, Any], accession: Mapping[str, Any] | None) -> tuple[str | None, str]:
    """(the unit's partner, why there is no single one): the one other unit of `accession` ({unit_id: payload}, the
    current payloads) that leaves out for polarity switching exactly the archive members this unit does, at the opposite
    polarity. (None, "") where there is none; (None, why) where one of another polarity, or more than one, does."""
    members, mine = _split_members(payload), _unit_polarity(payload)
    if not members or mine is None:
        return None, ""
    same = sorted(str(unit) for unit, other in (accession or {}).items()
                  if unit != payload.get("unit_id") and isinstance(other, Mapping) and other.get("polarity_split") is None
                  and _split_members(other) == members)
    theirs = POLARITY_SPLIT_MODES[1 - POLARITY_SPLIT_MODES.index(mine)]
    opposite = [unit for unit in same if _unit_polarity(accession[unit]) == theirs]  # type: ignore[index]
    if len(opposite) != len(same):
        return None, ("unit(s) " + ", ".join(unit for unit in same if unit not in opposite)
                      + f" leave out the same files at no {theirs} polarity")
    if len(opposite) > 1:
        return None, f"{len(opposite)} units of the other polarity leave out the same files: {', '.join(opposite)}"
    return (opposite[0] if opposite else None), ""


def polarity_own_refusal(payload: Mapping[str, Any] | None, accession: Mapping[str, Any] | None) -> str:
    """Why a payload does not run once at its own polarity under the decision of 2026-11-20, or "" where it does."""
    refusal = _polarity_hold_refusal(payload)
    if refusal:
        return refusal
    assert payload is not None
    ion = normalise_ion((payload.get("unit_technical") or {}).get("ion_mode"))
    partner, ambiguous = polarity_partner(payload, accession)
    if ambiguous:
        return ambiguous
    if ion in POLARITY_SPLIT_MODES or (ion == "switching" and partner is not None):
        return ""
    if ion == "switching":
        return "its unit ion mode is switching and no unit of the other polarity leaves out the same files"
    return f"its unit ion mode is {ion}, not positive, negative or switching beside a partner"


def polarity_split_refusal(payload: Mapping[str, Any] | None, accession: Mapping[str, Any] | None = None) -> str:
    """Why a parent payload is not split by polarity under the decision of 2026-11-19, or "" where it is. With
    `accession` ({unit_id: payload}), a unit that runs at its own polarity beside a partner (2026-11-20) is not split."""
    refusal = _polarity_hold_refusal(payload)
    if refusal:
        return refusal
    assert payload is not None
    ion = normalise_ion((payload.get("unit_technical") or {}).get("ion_mode"))
    if ion != "switching":
        return f"its unit ion mode is {ion}, not switching"
    if accession is not None and not polarity_own_refusal(payload, accession):
        return (f"it runs at its own polarity beside its partner {polarity_partner(payload, accession)[0]} "
                f"(the decision of {POLARITY_OWN_DECISION['date']})")
    return ""


def polarity_derivations(payload: Mapping[str, Any] | None, accession: Mapping[str, Any] | None
                         ) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """The children the decisions make of a parent, [(ion mode, derive_polarity_split options)]: two for a split
    (2026-11-19), one at its own polarity naming its partner (2026-11-20); ([], why) where neither reaches it."""
    if not polarity_split_refusal(payload, accession):
        return [(mode, {}) for mode in POLARITY_SPLIT_MODES], ""
    refusal = polarity_own_refusal(payload, accession)
    if refusal:
        return [], refusal
    assert payload is not None
    return [(str(_unit_polarity(payload)), {"partner_unit_id": polarity_partner(payload, accession)[0]})], ""


def accession_payloads(triage_dir: Path) -> dict[str, Any]:
    """The accession's current payloads by unit id (current_triage_files), as read; unreadable ones left out."""
    out: dict[str, Any] = {}
    for path in current_triage_files(Path(triage_dir)):
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("unit_id"), str) and "polarity_split" not in data:
            out[data["unit_id"]] = data
    return out


_SPLIT_IN_TWO: Any = object()


def derive_polarity_split(
    payload: Mapping[str, Any], ion_mode: str, *, triage_relative_path: str, triage_sha256: str,
    triage_record_sha256: str | None, partner_unit_id: Any = _SPLIT_IN_TWO,
) -> dict[str, Any]:
    """The child payload of `ion_mode` (positive or negative) of a parent that polarity_split_refusal passes; with
    `partner_unit_id` (a unit id or None), the one child of a parent that runs at its own polarity (2026-11-20)."""
    own = partner_unit_id is not _SPLIT_IN_TWO
    decision = POLARITY_OWN_DECISION if own else POLARITY_SPLIT_DECISION
    if ion_mode not in POLARITY_SPLIT_MODES:
        raise ValueError(f"a polarity-split child is positive or negative, not {ion_mode!r}")
    out = copy.deepcopy(dict(payload))
    technical = out["unit_technical"]
    taken = polarity_split_rows(payload)
    indices = {item["row_index"] for item in taken}
    kept = []
    for item in taken:
        row = {key: item[key] for key in ("row_index", "sample_id", "raw_file", "member_path")}
        row.update({
            "ion_mode": "switching",
            "chromatography": technical["chromatography"],
            "acquisition_mode": technical["acquisition_mode"],
            "basis": "declared",
            "evidence": [{"source": "catalog_row", "field": "raw_file", "value": item["raw_file"]}],
            "confidence": item["confidence"],
            "rationale": (f"Kept for the {ion_mode} run of a polarity-switching file under the author's decision of "
                          f"{decision['date']}; left out before as {item['reason']}: {item['detail']}"),
        })
        if item.get("rule_id"):
            row["rule_id"] = item["rule_id"]
        kept.append(row)
    out["rows"] = sorted([*out.get("rows", []), *kept], key=lambda row: row["row_index"])
    out["excluded_rows"] = [item for item in out.get("excluded_rows", []) if item.get("row_index") not in indices]
    out["status"] = "ready"
    out["needs_person"] = []
    technical["ion_mode"] = ion_mode
    out["polarity_split"] = {
        "ion_mode": ion_mode, "reason": POLARITY_SPLIT_REASON, "parent_unit_id": str(payload["unit_id"]),
        "sibling_ion_mode": POLARITY_SPLIT_MODES[1 - POLARITY_SPLIT_MODES.index(ion_mode)],
        **({"partner_unit_id": partner_unit_id} if own else {}),
    }
    out["derived_from"] = {
        "kind": "polarity_split", "triage_relative_path": triage_relative_path, "triage_sha256": triage_sha256,
        "triage_record_sha256": triage_record_sha256, "decision": dict(decision),
    }
    return out


def polarity_split_shape_problems(payload: Mapping[str, Any]) -> list[str]:
    """What is wrong with a payload's polarity_split and derived_from, judged on the payload alone."""
    split = payload.get("polarity_split")
    if not isinstance(split, Mapping):
        return ["polarity_split is not an object"]
    problems = []
    own = "partner_unit_id" in split
    keys = POLARITY_OWN_KEYS if own else POLARITY_SPLIT_KEYS
    decision = POLARITY_OWN_DECISION if own else POLARITY_SPLIT_DECISION
    if sorted(split) != sorted(keys):
        problems.append(f"polarity_split has keys {sorted(split)}, not {sorted(keys)}")
    partner = split.get("partner_unit_id")
    if own and partner is not None and (not isinstance(partner, str) or not partner or partner == payload.get("unit_id")):
        problems.append(f"polarity_split.partner_unit_id {partner!r} is neither null nor another unit's id")
    mode = split.get("ion_mode")
    if mode not in POLARITY_SPLIT_MODES:
        problems.append(f"polarity_split.ion_mode is {mode!r}, not positive or negative")
    elif split.get("sibling_ion_mode") != POLARITY_SPLIT_MODES[1 - POLARITY_SPLIT_MODES.index(mode)]:
        problems.append(f"polarity_split.sibling_ion_mode is {split.get('sibling_ion_mode')!r}; the sibling of a "
                        f"{mode} child is the other polarity")
    if split.get("reason") != POLARITY_SPLIT_REASON:
        problems.append(f"polarity_split.reason is {split.get('reason')!r}, not {POLARITY_SPLIT_REASON}")
    if split.get("parent_unit_id") != payload.get("unit_id"):
        problems.append(f"polarity_split.parent_unit_id {split.get('parent_unit_id')!r} is not the unit "
                        f"{payload.get('unit_id')!r}")
    if (payload.get("unit_technical") or {}).get("ion_mode") != mode:
        problems.append(f"unit_technical.ion_mode is {(payload.get('unit_technical') or {}).get('ion_mode')!r}, "
                        f"polarity_split.ion_mode {mode!r}")
    if payload.get("status") != "ready":
        problems.append(f"a polarity-split child is ready, not {payload.get('status')}")
    if payload.get("needs_person"):
        problems.append("a polarity-split child asks nothing")
    if payload.get("children"):
        problems.append("a polarity-split child has no children")
    if POLARITY_SPLIT_REASON not in {str(f.get("code")) for f in payload.get("flags") or [] if isinstance(f, Mapping)}:
        problems.append(f"a polarity-split child keeps its {POLARITY_SPLIT_REASON} flag")
    derived = payload.get("derived_from")
    if not isinstance(derived, Mapping) or sorted(derived) != sorted(DERIVED_FROM_KEYS) \
            or derived.get("kind") != "polarity_split" or derived.get("decision") != decision:
        problems.append("derived_from does not name the parent payload and the decision of "
                        f"{decision['date']} ({', '.join(DERIVED_FROM_KEYS)})")
    return problems


def check_polarity_split(report: "Report", unit: str, parent: Mapping[str, Any], parent_sha256: str,
                         parent_relative_path: str, record_sha256: str | None, child: Any,
                         accession: Mapping[str, Any] | None = None) -> bool:
    """Whether `child` may stand for its parent in the checks (T-SPLIT): a payload of the right shape that is exactly
    what derive_polarity_split makes of this parent, which a decision must reach. `accession` ({unit_id: payload}, the
    directory's current payloads) decides between a split in two and one child at the unit's own polarity."""
    if not isinstance(child, dict):
        report.add("error", "T-SPLIT", unit, "the polarity-split child is not one JSON object")
        return False
    if child.get("unit_id") != unit:
        report.add("error", "T-SPLIT", unit, f"the polarity-split child is of unit {child.get('unit_id')!r}, not {unit}")
        return False
    accession = {**(accession or {}), unit: parent}
    derivations, refusal = polarity_derivations(parent, accession)
    if refusal:
        report.add("error", "T-SPLIT", unit, f"the decision of {POLARITY_SPLIT_DECISION['date']} does not split this "
                   f"unit, nor does that of {POLARITY_OWN_DECISION['date']} run it at its own polarity: {refusal}")
        return False
    problems = polarity_split_shape_problems(child)
    for problem in problems:
        report.add("error", "T-SPLIT", unit, problem)
    if problems:
        return False
    options = dict(derivations).get(child["polarity_split"]["ion_mode"])
    if options is None or ("partner_unit_id" in options) != ("partner_unit_id" in child["polarity_split"]):
        made = ", ".join(mode + (f" at its own polarity (partner {o['partner_unit_id']})" if "partner_unit_id" in o else "")
                         for mode, o in derivations)
        report.add("error", "T-SPLIT", unit, f"the polarity-split child is not one the decisions make of its parent ({made})")
        return False
    expected = derive_polarity_split(parent, child["polarity_split"]["ion_mode"], triage_relative_path=parent_relative_path,
                                     triage_sha256=parent_sha256, triage_record_sha256=record_sha256, **options)
    if canonical_json(expected) != canonical_json(child):
        differ = sorted(key for key in set(expected) | set(child) if expected.get(key) != child.get(key))
        report.add("error", "T-SPLIT", unit, "the polarity-split child is not what the decision makes of its parent "
                   f"{parent_relative_path} (sha256 {parent_sha256}); it differs in {', '.join(differ)}")
        return False
    return True


# --------------------------------------------------------------------------------------------- findings


@dataclass
class Finding:
    level: str  # "error", "hold", or "note" (recorded, never a verdict)
    check: str
    unit: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "check": self.check, "unit": self.unit, "message": self.message}


@dataclass
class Report:
    accession: str | None = None
    units: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    unit_status: dict[str, str] = field(default_factory=dict)
    score: dict[str, dict[str, int]] | None = None
    payloads: dict[str, dict[str, Any]] = field(default_factory=dict)  # unit -> payload file name, schema, decision_id
    extra: dict[str, Any] = field(default_factory=dict)  # --emit-selection: selection, review_log

    def add(self, level: str, check: str, unit: str | None, message: str) -> None:
        self.findings.append(Finding(level, check, unit, message))

    def unit_verdict(self, unit: str) -> str:
        levels = {f.level for f in self.findings if f.unit == unit}
        return "error" if "error" in levels else "hold" if "hold" in levels else "pass"

    @property
    def verdict(self) -> str:
        levels = {f.level for f in self.findings}
        return "error" if "error" in levels else "hold" if "hold" in levels else "pass"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "accession": self.accession,
            "verdict": self.verdict,
            # The schema only: a payload's decision_id would tie every unit's report_sha256 to every other unit's bytes
            # (the plan records the report's sha256 per unit). --emit-selection reports the decision ids.
            "units": {u: {"status": self.unit_status.get(u), "verdict": self.unit_verdict(u),
                          "schema": self.payloads.get(u, {}).get("schema")} for u in self.units},
            "findings": [f.as_dict() for f in self.findings],
        }
        if self.score is not None:
            out["score"] = self.score
        out.update(self.extra)
        return out


# ------------------------------------------------------------------------------- minimal schema checker
# Every keyword the schemas under schemas/ use, so the result does not depend on which jsonschema package (if any) is
# installed. tests/test_validate_triage.py checks the schema itself against Draft 2020-12 when jsonschema is available,
# and tests/test_validate_triage_v2.py that no schema uses a keyword this checker does not implement.

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def schema_errors(value: Any, schema: dict[str, Any], root: dict[str, Any], where: str = "$") -> list[str]:
    if schema is True:
        return []
    errors: list[str] = []
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"unsupported $ref {ref}")
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part]
        errors += schema_errors(value, target, root, where)
    for sub in schema.get("allOf", []):
        errors += schema_errors(value, sub, root, where)
    if "anyOf" in schema and not any(not schema_errors(value, sub, root, where) for sub in schema["anyOf"]):
        errors.append(f"{where}: matches none of the allowed forms")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, n) for n in names):
            errors.append(f"{where}: expected {'/'.join(names)}, got {type(value).__name__}")
            return errors
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{where}: must not be empty")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: must be >= {schema['minimum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{where}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: at most {schema['maxItems']} item(s)")
        if schema.get("uniqueItems") and len({canonical_json(item) for item in value}) != len(value):
            errors.append(f"{where}: items must be unique")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += schema_errors(item, schema["items"], root, f"{where}[{i}]")
    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            errors.append(f"{where}: needs at least {schema['minProperties']} field(s); an empty object matches everything")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errors.append(f"{where}: at most {schema['maxProperties']} field(s)")
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing {key}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            if key in props:
                errors += schema_errors(item, props[key], root, f"{where}.{key}")
            elif extra is False:
                errors.append(f"{where}: unexpected field {key}")
            elif isinstance(extra, dict):
                if "propertyNames" in schema:
                    errors += schema_errors(key, schema["propertyNames"], root, f"{where} key {key!r}")
                errors += schema_errors(item, extra, root, f"{where}.{key}")
    return errors


def load_schema(schema_id: str = SCHEMA_ID) -> dict[str, Any]:
    """The JSON schema of a payload version (msdial-unit-triage.v1 or .v2) or of the decision record."""
    path = RECORD_SCHEMA_PATH if schema_id == RECORD_SCHEMA_ID else SCHEMA_PATHS[schema_id]
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------- inputs


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def member_key(path: str) -> str:
    """Case-insensitive key; members inside a vendor folder collapse to the folder."""
    p = _norm_path(path)
    m = VENDOR_FOLDER.match(p)
    if m:
        p = m.group(1)
    return p.casefold()


def basename(path: str) -> str:
    return _norm_path(path).rsplit("/", 1)[-1]


def stem(name: str) -> str:
    return re.sub(r"(?i)\.(raw|mzml|mzxml|mzdata|wiff2?|d|lcd|cdf)$", "", basename(name))


def _name_key(raw_file: str) -> str:
    name = basename(raw_file)
    return ("stem:" + name.casefold()) if stem(name) == name else ("name:" + name.casefold())


def is_raw_like(path: str) -> bool:
    p = _norm_path(path)
    b = basename(p)
    if p.startswith("__MACOSX/") or b.startswith("._"):
        return False
    return b.casefold().endswith(RAW_EXTENSIONS) or VENDOR_FOLDER.match(p) is not None


@dataclass
class ListedMember:
    archive: str | None
    path: str
    size: int
    crc32: str


def read_listing(path: Path) -> list[ListedMember]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = {name.lower(): name for name in (reader.fieldnames or [])}
        crc_field = fields.get("crc32") or fields.get("crc")
        if "path" not in fields or "size" not in fields or crc_field is None:
            raise ValueError(f"{path}: an archive listing needs path, size and crc32 columns")
        out = []
        for row in reader:
            if "type" in fields and row[fields["type"]] not in ("file", ""):
                continue
            out.append(
                ListedMember(
                    archive=row.get(fields["archive"]) if "archive" in fields else None,
                    path=_norm_path(row[fields["path"]]),
                    size=int(row[fields["size"]] or 0),
                    crc32=(row[crc_field] or "").upper(),
                )
            )
        return out


@dataclass
class CatalogUnit:
    unit_id: str
    rows: list[dict[str, Any]]
    declared: dict[str, Any]
    handoff_sha256: str | None
    accession: str | None = None
    problem: str | None = None  # a handoff released as a projection whose bytes are not the ones the index names


def read_catalog(path: Path, bundle: "ReleaseBundle | None" = None) -> dict[str, CatalogUnit]:
    """The Catalog units a handoffs directory or a catalog.json holds. In a release bundle a handoff.json released as a
    projection (release-index.json triage_inputs) stands for its original: its sha256 is the index's original_sha256,
    once its bytes are checked against the index's release_sha256 (a mismatch is the unit's `problem`)."""
    units: dict[str, CatalogUnit] = {}
    if path.is_dir():
        for handoff in sorted(path.glob("*/handoff.json")):
            unit_dir = handoff.parent
            raw = handoff.read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
            handoff_sha = hashlib.sha256(raw).hexdigest()
            problem = None
            entry = bundle.projected(handoff) if bundle is not None else None
            if entry is not None:
                if handoff_sha != entry["release_sha256"]:
                    problem = (f"{handoff.name} of {unit_dir.name} hashes to {handoff_sha}, not the release index's "
                               f"release_sha256 {entry['release_sha256']}; its original sha256 is not taken on trust")
                handoff_sha = entry["original_sha256"]
            samples_path = unit_dir / "samples.json"
            rows = json.loads(samples_path.read_text(encoding="utf-8")) if samples_path.exists() else data.get("sample_metadata") or []
            ts = data.get("technical_settings") or {}
            units[unit_dir.name] = CatalogUnit(
                unit_id=unit_dir.name,
                rows=rows,
                declared={k: ts.get(k) for k in ("chromatography", "ion_mode", "acquisition_mode")},
                handoff_sha256=handoff_sha,
                accession=data.get("accession"),
                problem=problem,
            )
        return units
    data = json.loads(path.read_text(encoding="utf-8"))
    for unit in data["units"]:
        units[unit["unit_id"]] = CatalogUnit(
            unit_id=unit["unit_id"],
            rows=unit["rows"],
            declared=unit.get("declared") or {},
            handoff_sha256=unit.get("handoff_sha256"),
            accession=unit.get("accession") or data.get("accession"),
        )
    return units


# ------------------------------------------------------------------------------------ normalisation

_POLARITY_TOKEN = re.compile(r"(?i)(positive|negative|pos|neg)(?![a-z])")
_HILIC_TOKEN = re.compile(r"(?i)hilic")
_RP_TOKEN = re.compile(r"(?i)((?<![a-z])c(?:18|30|8)(?![0-9])|rplc|(?<![a-z])rp(?![a-z])|rp(?=pos|neg))")


def normalise_ion(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v.startswith("pos"):
        return "positive"
    if v.startswith("neg"):
        return "negative"
    if v in ("both", "positive and negative", "mixed"):
        return "both"
    if v in ("switching", "polarityswitching", "polarity switching", "polarity_switching"):
        return "switching"
    return "unknown"


def normalise_chromatography(value: Any) -> str:
    v = str(value or "").strip().lower()
    if "hilic" in v:
        return "HILIC"
    if v in ("rp", "rplc") or "reverse" in v or "c18" in v or "c8" in v or "c30" in v:
        return "RP"
    if "normal" in v:
        return "normal_phase"
    return "unknown" if not v or v == "unknown" else "other"


def token_polarity(path: str) -> str | None:
    found = {("positive" if m.group(1).lower().startswith("pos") else "negative") for m in _POLARITY_TOKEN.finditer(_norm_path(path))}
    return found.pop() if len(found) == 1 else None


def token_chromatography(path: str) -> str | None:
    p = _norm_path(path)
    found = set()
    if _HILIC_TOKEN.search(p):
        found.add("HILIC")
    if _RP_TOKEN.search(p):
        found.add("RP")
    return found.pop() if len(found) == 1 else None


# Acquisition tokens a name may carry (the digest prints them, the spec expander compares them within a group). A
# token is a fact about the name, not about the file: the headers settle what was acquired. A token starts where a word
# starts: after a character that is not a letter, or after a lower-case letter when the token is written in capitals
# (posDDA), so that Media, media1 or Claudia carry no DIA. PRM, SRM, MRM, SIM and tMS2 name a targeted method, which
# the campaign does not run (TARGETED_TOKENS); tDDA, HERMES and targeted ddMS2 name inclusion-list DDA, which it does.
# Waters MSe and HDMSe name DIA (product ions in the high-energy function).
_ACQ_TOKEN = re.compile(r"(?i)(t-?dd-?ms2?|t-?dda|targeted-?dd-?ms2?|hermes|dd-?ms2?|dda|dia[12](?![0-9])|dia|swath|aif|(?:hd)?mse|"
                        r"scan0[1-9](?![0-9])|ms1(?![0-9])|full-?scan|prm|srm|mrm|sim|tms2)(?![a-z])")
TARGETED_TOKENS = ("MRM", "PRM", "SIM", "SRM", "tMS2")
INCLUSION_LIST_TOKENS = ("tDDA",)
# The acquisition_mode a name token says (T-TOKEN); SCANnn and the targeted tokens say none a payload can hold.
TOKEN_MODES = {"DDA": "DDA", "tDDA": "DDA", "DIA": "DIA", "DIA1": "DIA", "DIA2": "DIA", "SWATH": "SWATH", "AIF": "AIF",
               "MSe": "DIA", "MS1": "MS1"}
DIA_FAMILY = {"DIA", "SWATH", "AIF"}


def _acq_name(token: str) -> str:
    t = token.casefold().replace("-", "")
    if t.startswith(("tdd", "targeted", "hermes")):
        return "tDDA"
    if t.startswith(("ddms", "dda")):
        return "DDA"
    if t in ("ms1", "fullscan"):
        return "MS1"
    if t == "tms2":
        return "tMS2"
    if t in ("mse", "hdmse"):
        return "MSe"
    return t.upper()


def _starts_a_word(text: str, match: re.Match[str]) -> bool:
    before = text[match.start() - 1] if match.start() else ""
    if not before.isalpha():
        return True
    letters = [c for c in match.group(1) if c.isalpha()]
    return before.islower() and all(c.isupper() for c in letters[1:]) and letters[0].isupper()


def token_acquisition(path: str) -> tuple[str, ...]:
    """Every acquisition token the path carries (DDA, DIA, DIA1, DIA2, SWATH, AIF, MSe, SCAN01..09, MS1, tDDA, and the
    targeted MRM, PRM, SIM, SRM, tMS2), sorted."""
    text = _norm_path(path)
    return tuple(sorted({_acq_name(m.group(1)) for m in _ACQ_TOKEN.finditer(text) if _starts_a_word(text, m)}))


# Words that name an MS level, as a name writes them (triage-digest.py section g, and the P1 guard of T-EXCL). A word
# starts after a non-letter or, in capitals, after a lower-case letter (posMS, CSHnegMSMS), as acquisition tokens do.
# MS/MS words name runs with product-ion spectra: MSMS and AutoMSMS, MS2 and ddMS2, Waters MSe and HDMSe (DIA, product
# ions in the high-energy function), SONAR, MSn, and SCIEX IDA. Bare MS is matched in capitals only, not before another
# capital or a digit (MSDIAL, MS2 and MSMS are not it) and not as the MS of MSe or MSn; the others ignore case.
_MS_LEVEL_WORDS = (
    ("MS/MS", "MSMS", re.compile(r"(?i)(auto-?ms-?ms|ms-?ms)")),
    ("MS/MS", "MS2", re.compile(r"(?i)((?:dd-?)?ms2)(?![0-9])")),
    ("MS/MS", "MSe", re.compile(r"((?:HD)?MS[eE])(?![A-Za-z0-9])")),
    ("MS/MS", "SONAR", re.compile(r"(?i)(sonar)(?![a-z])")),
    ("MS/MS", "MSn", re.compile(r"(MSn)(?![A-Za-z0-9])")),
    ("MS/MS", "IDA", re.compile(r"(IDA)(?![A-Za-z0-9])")),
    ("MS-only", "MSProfile", re.compile(r"(?i)(ms-?prof(?:ile)?)")),
    ("MS-only", "Profile", re.compile(r"(?i)(prof(?:ile)?)(?![a-z])")),
    ("MS-only", "MS1", re.compile(r"(?i)(ms1)(?![0-9])")),
    ("MS-only", "FullScan", re.compile(r"(?i)(full-?scan)")),
    ("MS-only", "MS", re.compile(r"(MS)(?![A-Z0-9])(?![en](?![a-z]))")),
)


def ms_level_words(name: str) -> list[tuple[str, str]]:
    """(kind, word) for each MS-level word of a name ("MS-only" or "MS/MS"), longest first, each span read once."""
    taken: list[tuple[int, int]] = []
    found: list[tuple[str, str]] = []
    for kind, label, pattern in _MS_LEVEL_WORDS:
        for match in pattern.finditer(name):
            start, end = match.span(1)
            if any(start < b and a < end for a, b in taken):
                continue
            before = name[start - 1] if start else ""
            first = match.group(1)[0]
            if before.isalpha() and not (before.islower() and first.isupper()):
                continue
            taken.append((start, end))
            found.append((kind, label))
    return found


def product_ion_words(path: str) -> list[str]:
    """What a file's own name (the stem of its basename) says of product-ion spectra: its MS/MS words (MSMS, MS2, MSe,
    ...) and the DDA, DIA, SWATH or AIF modes its tokens name, sorted; empty when it says none."""
    name = stem(basename(_norm_path(path)))
    words = {label for kind, label in ms_level_words(name) if kind == "MS/MS"}
    words |= {TOKEN_MODES[t] for t in token_acquisition(name) if TOKEN_MODES.get(t) not in (None, "MS1")}
    return sorted(words)


# --------------------------------------------------------------------------------------- the checks


CONVERSION_EXTENSIONS = (".mzxml", ".mzdata")


def _key_stem(key: str) -> str:
    return key[4:] if key.startswith("raw:") else stem(key).casefold()


class Resolver:
    """Gives every file one key, whether a triage names it by member path or only by the row's raw_file.

    The index is the archive listing when there is one, and otherwise every member path the triage files
    name. A name resolves to a member by its exact file name, then by its stem, then by its stem among
    members that need no conversion; anything else stays "raw:<stem>" (unresolved).
    """

    def __init__(self, members: Iterable[str]):
        self.members: set[str] = set()
        self.original: dict[str, str] = {}  # key -> the member path as the listing or triage spells it
        self.by_name: dict[str, set[str]] = defaultdict(set)
        self.by_stem: dict[str, set[str]] = defaultdict(set)
        for path in members:
            if not is_raw_like(path):
                continue
            key = member_key(path)
            self.members.add(key)
            norm = _norm_path(path)
            folder = VENDOR_FOLDER.match(norm)
            self.original.setdefault(key, folder.group(1) if folder else norm)
            self.by_name[basename(key)].add(key)
            self.by_stem[stem(key).casefold()].add(key)

    def hits(self, name: str) -> set[str]:
        """Every indexed member the name could mean: same file name, or failing that, same stem."""
        return set(self.by_name.get(basename(name).casefold(), ())) or set(self.by_stem.get(stem(name).casefold(), ()))

    def resolve(self, name: str) -> str | None:
        found = self.by_name.get(basename(name).casefold(), set())
        if len(found) == 1:
            return next(iter(found))
        found = self.by_stem.get(stem(name).casefold(), set())
        if len(found) > 1:
            found = {k for k in found if not k.endswith(CONVERSION_EXTENSIONS)}
        return next(iter(found)) if len(found) == 1 else None

    def key(self, path: str) -> tuple[str, bool]:
        """(key, resolved) for a member path or a bare file name."""
        mk = member_key(path)
        if mk in self.members or "/" in _norm_path(path):
            return mk, True
        found = self.resolve(path)
        return (found, True) if found else ("raw:" + stem(path).casefold(), False)


@dataclass
class Kept:
    """One file a unit keeps: a Catalog row or an added member."""

    unit: str
    key: str
    path: str  # member path, or the row's raw_file when the member is unknown
    item: dict[str, Any]
    is_row: bool
    resolved: bool = True  # False: a row with no member_path whose raw_file names no single member
    member: str | None = None  # the member path the file resolves to (None when unresolved)

    @property
    def stem(self) -> str:
        return _key_stem(self.key)


def _kept(unit: str, triage: dict[str, Any], resolver: Resolver) -> list[Kept]:
    out = []
    for row in triage.get("rows", []):
        if row.get("member_path"):
            out.append(Kept(unit, member_key(row["member_path"]), row["member_path"], row, True, True, row["member_path"]))
        else:
            key, resolved = resolver.key(row.get("raw_file") or "")
            member = resolver.original.get(key, key) if resolved else None
            out.append(Kept(unit, key, row.get("raw_file") or "", row, True, resolved, member))
    for member in triage.get("added_members", []):
        out.append(Kept(unit, member_key(member["member_path"]), member["member_path"], member, False, True, member["member_path"]))
    return out


def same_file(kept: Kept, path: str, resolver: Resolver) -> bool:
    """Does path (a member path or a bare file name) name the file kept is? Unresolved names match by stem."""
    key, resolved = resolver.key(path)
    if key == kept.key:
        return True
    return (not resolved or not kept.resolved) and _key_stem(key) == kept.stem


def _extension(key: str) -> str:
    m = re.search(r"(?i)\.(raw|mzml|mzxml|mzdata|wiff2?|d|lcd|cdf)$", key)
    return m.group(1).casefold() if m else ""


def _runnable_form(item: dict[str, Any], resolver: Resolver) -> str | None:
    """For an exclusion as requires_conversion: the form of the file MS-DIAL reads as it is, or None.

    A member path is read as it is named. A row named only by raw_file is read through the index: the
    member it resolves to (which prefers a vendor or mzML form over mzXML and mzData), else every member of
    its name or stem, else the raw_file's own extension.
    """
    def runnable(path: str) -> bool:
        return is_raw_like(path) and not _norm_path(path).casefold().endswith(CONVERSION_EXTENSIONS)

    if item.get("member_path"):
        return item["member_path"] if runnable(item["member_path"]) else None
    name = item.get("raw_file") or ""
    key = resolver.resolve(name)
    if key is not None:
        return resolver.original.get(key, key) if runnable(key) else None
    hits = resolver.hits(name)
    if hits:
        return None if any(not runnable(h) for h in hits) else resolver.original.get(min(hits), min(hits))
    return name if runnable(name) else None


def _triage_member_paths(triages: dict[str, dict[str, Any]]) -> list[str]:
    paths = []
    for t in triages.values():
        for item in [*t.get("rows", []), *t.get("excluded_rows", []), *t.get("added_members", []), *t.get("excluded_members", [])]:
            if item.get("member_path"):
                paths.append(item["member_path"])
        for child in t.get("children", []):
            paths += child["member_paths"]
    return paths


def _flag_codes(triage: dict[str, Any]) -> set[str]:
    return {f["code"] for f in triage.get("flags", [])}


_REVISION = re.compile(r"^(?P<unit>.+)\.r(?P<n>[0-9]+)$")
RECORD_SUFFIX = ".record.json"
# The agent's compact spec (<accession>.spec.json, a revision <accession>.spec.r<N>.json; schemas/
# msdial-unit-triage-spec.v1.json): bound by the records and expanded into the payloads, never a payload itself.
SPEC_FILE = re.compile(r"^(?P<accession>.+)\.spec(?:\.r(?P<n>[0-9]+))?\.json$")


def is_spec_file(name: str) -> bool:
    return SPEC_FILE.match(name) is not None


def is_payload_file(name: str) -> bool:
    """A triage directory's *.json that is a payload: not index.json, a decision record or a spec."""
    return name.endswith(".json") and name != "index.json" and not name.endswith(RECORD_SUFFIX) and not is_spec_file(name)


def triage_file_unit(path: Path) -> str:
    """The unit a triage file is named for: <unit>.json, or a revision <unit>.r<N>.json."""
    match = _REVISION.match(path.stem)
    return match.group("unit") if match else path.stem


def current_triage_files(triage_dir: Path) -> list[Path]:
    """The triage files of a directory as the campaign keeps them (scripts/campaign/curated.py): every *.json but
    index.json and the decision records (<name>.record.json, the envelope beside a payload), and of a unit with
    revisions (<unit>.r<N>.json) only the highest, which supersedes <unit>.json and every earlier revision."""
    current: dict[str, tuple[int, Path]] = {}
    for path in sorted(triage_dir.glob("*.json")):
        if not is_payload_file(path.name):
            continue
        match = _REVISION.match(path.stem)
        unit, number = (match.group("unit"), int(match.group("n"))) if match else (path.stem, 0)
        if unit not in current or number > current[unit][0]:
            current[unit] = (number, path)
    return sorted(path for _number, path in current.values())


# ------------------------------------------------------------------------------------ the Class is not the triage's
# WS0 decision F (2026-11-17): the triage runs before any Class is saved and binds the evidence it read (the Catalog's
# rows, files and listing); the Class binds through its own ratification afterwards. A triage that cites the Class
# (the handoff's class_proposal: its assignments, labels, contrast, status or ratification) would make the selection
# depend on a grouping the triage does not bind, so it is an error: the triage is redone without it.
CLASS_CONTENT = re.compile(r"(?i)class[ _.-]?proposal|class[ _.-]?assignment|class_label|ratified_by|ratification")


def _citations(value: Any) -> Iterable[dict[str, Any]]:
    """Every evidence citation in a triage document: an object naming a source (evidence, evidence_sources)."""
    if isinstance(value, dict):
        if isinstance(value.get("source"), str):
            yield value
        for item in value.values():
            yield from _citations(item)
    elif isinstance(value, list):
        for item in value:
            yield from _citations(item)


def class_citations(triage: Any) -> list[str]:
    """What in a triage document cites the Class: an evidence citation whose field, value or detail names the
    class_proposal or its content. The validator reads no class_proposal itself."""
    found: list[str] = []
    for citation in _citations(triage):
        for key in ("field", "value", "detail"):
            text = citation.get(key)
            if isinstance(text, str) and CLASS_CONTENT.search(text):
                found.append(f"evidence from {citation.get('source')} cites the Class in its {key} ({text[:80]!r})")
                break
    return found


def record_class_citations(triage_path: Path) -> list[str]:
    """What the decision record beside a triage (<name>.record.json) binds of the Class: an input whose role, path or
    selector names the class_proposal. Only the inputs are read; a record that is missing or not JSON is left to the
    plan's own checks."""
    record = triage_path.with_name(triage_path.name[: -len(".json")] + RECORD_SUFFIX)
    try:
        value = json.loads(record.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    found: list[str] = []
    for item in (value.get("inputs") if isinstance(value, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        for key, text in item.items():
            if key not in ("sha256", "bytes") and isinstance(text, str) and CLASS_CONTENT.search(text):
                found.append(f"its decision record {record.name} binds an input whose {key} names the Class ({text[:80]!r})")
                break
    return found


# ------------------------------------------------------------------------------ the decision record (section 2.3)
# The model writes only the payload; the wrapper writes the envelope from the harness's transcript. Everything the
# envelope names by sha256 is recomputed here from the stored bytes. Nothing here runs a model or reads a Class.


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8 (scripts/campaign/curated.canonical): the form every digest here is taken over."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


class CodeSource:
    """The repository files a record names at its repo_commit (SKILL.md, the prompt template).

    By default `git show <commit>:<path>` in the repository this script belongs to, so a record is checked against the
    exact bytes it names; with --code-root, the file at <code root>/<path> (the released code, at its commit, for a third
    party without the repository). read() gives (bytes, None) or (None, why it could not be read)."""

    def __init__(self, code_root: Path | None = None, repo: Path = REPO_ROOT):
        self.code_root = Path(code_root) if code_root is not None else None
        self.repo = repo
        self._cache: dict[tuple[str, str], tuple[bytes | None, str | None]] = {}

    def read(self, commit: str, path: str) -> tuple[bytes | None, str | None]:
        relative = str(path or "").replace("\\", "/").strip("/")
        if not relative or _ABSOLUTE.match(str(path)) or ".." in relative.split("/"):
            return None, f"{path!r} is not a path relative to the repository"
        key = (commit, relative)
        if key not in self._cache:
            self._cache[key] = self._read(commit, relative)
        return self._cache[key]

    def _read(self, commit: str, relative: str) -> tuple[bytes | None, str | None]:
        if self.code_root is not None:
            try:
                return (self.code_root / relative).read_bytes(), None
            except OSError as error:
                return None, f"{relative} is not in the code root ({type(error).__name__})"
        if not re.fullmatch(r"[0-9a-f]{40}", str(commit or "")):
            return None, f"repo_commit {commit!r} is not a commit id"
        try:
            done = subprocess.run(["git", "-C", str(self.repo), "show", f"{commit}:{relative}"],
                                  capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return None, f"git could not run ({type(error).__name__})"
        if done.returncode != 0:
            return None, f"{relative} at {commit[:12]} is not in this repository"
        return done.stdout, None


def code_commit(repo: Path = REPO_ROOT) -> str | None:
    """The commit this validator runs at: HEAD, when the running script is the one HEAD holds (line endings aside);
    None when it is not a commit's (an edited working tree) or git cannot say."""
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, timeout=60, check=False)
        if head.returncode != 0:
            return None
        commit = head.stdout.decode("ascii", "replace").strip()
        shown = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:scripts/validate-triage.py"],
                               capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    running = Path(__file__).read_bytes().replace(b"\r\n", b"\n")
    if shown.returncode != 0 or shown.stdout.replace(b"\r\n", b"\n") != running:
        return None
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def skill_frontmatter(text: str) -> dict[str, Any]:
    """The YAML front matter of a SKILL.md, as far as this contract reads it: scalars, and lists written either inline
    (a, b) or as '- item' lines. Nothing beyond that is parsed."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, Any] = {}
    key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        item = re.match(r"^\s+-\s+(.*\S)\s*$", line)
        if item and key is not None:
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(item.group(1).strip("'\""))
            continue
        pair = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if pair:
            key, value = pair.group(1), pair.group(2)
            out[key] = value.strip("'\"") if value else None
    return out


def skill_allowed_tools(text: str) -> list[str] | None:
    """The skill's allowed-tools, in order, or None when it names none."""
    value = skill_frontmatter(text).get("allowed-tools")
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    return list(value) if value else None


def skill_version(text: str) -> str | None:
    match = re.search(r"Skill version: \*\*([0-9]+\.[0-9]+\.[0-9]+)\*\*", text)
    return match.group(1) if match else None


_MCP_TOOL = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>.+)$")


def tool_server(name: str) -> str | None:
    match = _MCP_TOOL.match(name)
    return match.group("server") if match else None


def bare_tool(name: str) -> str:
    """A tool's own name: mcp__<server>__<tool> is <tool>; a permission rule's '(...)' is dropped."""
    base = str(name or "").split("(", 1)[0].strip()
    match = _MCP_TOOL.match(base)
    return match.group("tool") if match else base


def tool_allowed(name: str, allowed: Iterable[str]) -> bool:
    """A tool call against the skill's allowed-tools: a server-qualified name (mcp__<server>__<tool>) must be listed as
    it is; a bare name (as a record may write an MCP tool, msdial_catalog_get_analysis_unit) matches a listed tool of
    that name."""
    allowed_bases = [str(item).split("(", 1)[0].strip() for item in allowed]
    base = str(name or "").split("(", 1)[0].strip()
    if _MCP_TOOL.match(base):
        return base in allowed_bases
    return base in allowed_bases or base in {bare_tool(item) for item in allowed_bases}


def _result_text(content: Any) -> str:
    """The text of a tool result's content: a string, or the text of its content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
    return ""


def _stored_result(data: bytes | None) -> dict[str, Any] | None:
    """A stored tool result ({tool_use_id, tool, is_error, content}) from its bytes, or None."""
    if data is None:
        return None
    try:
        stored = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    return stored if isinstance(stored, dict) else None


def harness_refused(name: str, result: Mapping[str, Any] | None) -> bool:
    """A tool result that is the harness's own refusal of a tool it does not offer (`No such tool available: <name>`):
    the call never reached a tool. A model may still ask for a tool it was not given (Edit, to change the spec it
    wrote); the attempt changes nothing, so it is a note, not a skill violation. Anything else that answers a call to a
    tool the skill does not allow is an error."""
    if not isinstance(result, Mapping) or not result.get("is_error"):
        return False
    text = _result_text(result.get("content"))
    return re.search(r"No such tool available: " + re.escape(str(name)) + r"(?![\w-])", text) is not None


def transcript_tool_names(data: bytes) -> list[str]:
    """The tool calls a harness transcript (JSON lines) shows, in order: every {"type": "tool_use", "name": ...} block,
    once per id."""
    names: list[str] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "tool_use" and isinstance(value.get("name"), str):
                ident = value.get("id")
                if not (isinstance(ident, str) and ident in seen):
                    if isinstance(ident, str):
                        seen.add(ident)
                    names.append(value["name"])
                return
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return names


class ReleaseBundle:
    """A release bundle (section 5.3): its root and the triage inputs release-index.json says were released as
    projections, by bundle-relative path."""

    def __init__(self, root: Path):
        self.root = Path(root)
        index = json.loads((self.root / RELEASE_INDEX).read_text(encoding="utf-8-sig"))
        self.inputs: dict[str, dict[str, str]] = {}
        for item in index.get("triage_inputs") or []:
            if isinstance(item, dict) and item.get("release"):
                self.inputs[str(item["release"]).replace("\\", "/")] = {
                    "original_sha256": str(item.get("original_sha256") or "").removeprefix("sha256:").casefold(),
                    "release_sha256": str(item.get("release_sha256") or "").removeprefix("sha256:").casefold(),
                }

    @staticmethod
    def find(triage_dir: Path) -> "ReleaseBundle | None":
        """The bundle a triage directory (<bundle>/triage/<accession>) sits in, or None."""
        root = Path(triage_dir).resolve().parent.parent
        return ReleaseBundle(root) if (root / RELEASE_INDEX).is_file() else None

    def projected(self, path: Path) -> dict[str, str] | None:
        try:
            relative = Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return None
        return self.inputs.get(relative)


@dataclass
class Provenance:
    """What the record checks need beyond the triage directory."""

    code: CodeSource = field(default_factory=CodeSource)
    bundle: ReleaseBundle | None = None
    # The sha256 of each --archive-listing file given (None when the caller gave listings without their files), and
    # the listings the records bind, so that the listings checked are the ones the decisions rested on.
    listing_sha256: set[str] | None = None
    bound_listings: set[str] = field(default_factory=set)
    modules: dict[str, tuple[Any, str | None]] = field(default_factory=dict)
    expansions: dict[str, Any] = field(default_factory=dict)
    # Why every v2 payload of the accession must be expanded from a spec (a spec in the directory, a record that binds
    # one), or None; and per unit every payload whose record binds a derivation: [(spec revision number, payload)].
    derived_accession: str | None = None
    derived: dict[str, list[tuple[int, dict[str, Any]]]] = field(default_factory=dict)
    spec_units: dict[str, set[str]] = field(default_factory=dict)  # spec sha256 -> the units whose records bind it

    def code_matches(self, data: bytes, expected: str) -> bool:
        """`data` (a file the record names at its commit) hashes to `expected`; from a released code folder, also once
        its line endings are LF, since a checkout with core.autocrlf writes CRLF where the commit holds LF."""
        if sha256_bytes(data) == expected:
            return True
        return self.code.code_root is not None and sha256_bytes(data.replace(b"\r\n", b"\n")) == expected


def transcript_events(data: bytes) -> dict[str, Any]:
    """A harness transcript (stream JSON lines) as a record is checked against it: {init, calls: [{id, name, input,
    result}] in order, once per id, each tool_result paired with its call, models (the init's and every assistant
    message's)}."""
    init: dict[str, Any] | None = None
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    models: list[str] = []
    for line in data.decode("utf-8-sig", "replace").splitlines():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("type") == "system" and message.get("subtype") == "init" and init is None:
            init = message
            if message.get("model") and str(message["model"]) not in models:
                models.append(str(message["model"]))
        body = message.get("message") if isinstance(message.get("message"), dict) else {}
        if message.get("type") == "assistant" and body.get("model") and str(body["model"]) not in models:
            models.append(str(body["model"]))
        for block in body.get("content") if isinstance(body.get("content"), list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                if block["id"] not in calls:
                    order.append(block["id"])
                    calls[block["id"]] = {"id": block["id"], "name": str(block.get("name") or ""),
                                          "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                                          "result": None}
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                calls[block["tool_use_id"]]["result"] = block
    return {"init": init, "calls": [calls[i] for i in order], "models": models}


def _file_check(report: "Report", unit: str, record: Path, label: str, path_text: Any, expected_sha: Any,
                expected_bytes: Any, bundle: ReleaseBundle | None, check: str = "T-RECORD") -> bytes | None:
    """One file a record names, against the sha256 (and bytes) it records. A file released as a projection is checked
    through the release index. Returns its bytes when they could be read."""
    text = str(path_text or "")
    if not text:
        report.add("error", check, unit, f"{record.name}: {label} names no path")
        return None
    if _ABSOLUTE.match(text):
        report.add("error", check, unit, f"{record.name}: {label} is an absolute path; a record names its files relative "
                   "to itself, because it is released byte for byte")
        return None
    location = record.parent / text
    try:
        data = location.read_bytes()
    except OSError:
        report.add("error", check, unit, f"{record.name}: {label} {text} is not there; the decision cannot be checked "
                   "against what it rested on")
        return None
    expected = str(expected_sha or "").casefold()
    entry = bundle.projected(location) if bundle is not None else None
    if entry is not None:
        if sha256_bytes(data) != entry["release_sha256"]:
            report.add("error", check, unit, f"{record.name}: {label} {text} is released as a projection, but hashes to "
                       f"{sha256_bytes(data)}, not the release index's release_sha256 {entry['release_sha256']}")
        elif entry["original_sha256"] != expected:
            report.add("error", check, unit, f"{record.name}: {label} {text}: the release index's original_sha256 "
                       f"{entry['original_sha256']} is not the {expected or '(none)'} the record names")
        return data
    if sha256_bytes(data) != expected:
        report.add("error", check, unit, f"{record.name}: {label} {text} hashes to {sha256_bytes(data)}; the record names "
                   f"{expected or '(none)'}")
    if expected_bytes is not None and len(data) != expected_bytes:
        report.add("error", check, unit, f"{record.name}: {label} {text} is {len(data)} bytes; the record names {expected_bytes}")
    return data


def check_record(report: "Report", unit: str, triage_dir: Path, payload_path: Path, payload_bytes: bytes,
                 payload: dict[str, Any], provenance: Provenance) -> dict[str, Any] | None:
    """The decision record beside one payload (section 2.3). Returns the record when it could be read as one."""
    record_path = payload_path.with_name(payload_path.name[: -len(".json")] + RECORD_SUFFIX)
    v2 = payload.get("schema") == SCHEMA_V2_ID
    if not record_path.is_file():
        if v2:
            report.add("hold", "T-RECORD", unit, f"{payload_path.name} has no decision record ({record_path.name}); what it "
                       "rested on, the skill, the prompt and the tool calls cannot be checked")
        return None
    try:
        record = json.loads(record_path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        report.add("error", "T-RECORD", unit, f"{record_path.name} is not readable JSON: {error}")
        return None
    schema = load_schema(RECORD_SCHEMA_ID)
    problems = schema_errors(record, schema, schema)
    if problems:
        for problem in problems[:20]:
            report.add("error", "T-RECORD", unit, f"{record_path.name}: {problem}")
        if len(problems) > 20:
            report.add("error", "T-RECORD", unit, f"{record_path.name}: {len(problems) - 20} more schema problem(s)")
        return None
    bundle = provenance.bundle
    sha = sha256_bytes(payload_bytes)
    named = record["payload"]
    if record["decision_id"] != sha:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: decision_id {record['decision_id']} is not the payload's "
                   f"sha256 {sha}")
    if named["sha256"] != sha:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: payload.sha256 {named['sha256']} is not the payload's "
                   f"sha256 {sha}")
    if named["path"] != payload_path.name:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: payload.path {named['path']!r} is not {payload_path.name}")
    if named["schema"] != payload.get("schema"):
        report.add("error", "T-RECORD", unit, f"{record_path.name}: payload.schema {named['schema']!r} is not the payload's "
                   f"{payload.get('schema')!r}")
    if record["kind"] != "unit_triage":
        report.add("error", "T-RECORD", unit, f"{record_path.name}: kind {record['kind']!r} beside a triage payload")
    if record["accession"] != payload.get("accession") or record["unit_id"] != payload.get("unit_id"):
        report.add("error", "T-RECORD", unit, f"{record_path.name}: names {record['accession']}/{record['unit_id']}, the "
                   f"payload {payload.get('accession')}/{payload.get('unit_id')}")
    if record["supersedes"] == sha:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: a decision cannot supersede itself")

    for index, item in enumerate(record["inputs"]):
        _file_check(report, unit, record_path, f"inputs[{index}] ({item['role']})", item["path"], item["sha256"],
                    item["bytes"], bundle)

    # What the decision rested on must be bound: the unit's Catalog evidence and the archive listing. A record that
    # binds neither states nothing a third party can check, so it never passes.
    if record["kind"] == "unit_triage":
        roles = [item["role"] for item in record["inputs"]]
        if not set(roles) & set(CATALOG_ROLES):
            report.add("hold", "T-RECORD", unit, f"{record_path.name} binds no Catalog evidence ({', '.join(CATALOG_ROLES)}); "
                       "the rows the triage covers cannot be traced to what it read")
        listings = [item for item in record["inputs"] if item["role"] == "archive_listing"]
        if not listings:
            report.add("hold", "T-RECORD", unit, f"{record_path.name} binds no archive listing; the selection's crc32 and "
                       "size, and the byte-identical content check, have no bound source")
        for item in listings:
            provenance.bound_listings.add(str(item["sha256"]).casefold())
            if provenance.listing_sha256 is not None and str(item["sha256"]).casefold() not in provenance.listing_sha256:
                report.add("hold", "T-RECORD", unit, f"{record_path.name} binds the listing {item['path']}, which is not "
                           "among the --archive-listing files given; the checks ran on listings the decision did not rest on")
        own = [item for item in record["inputs"] if item["role"] == "catalog_handoff"
               and str(item["path"]).replace("\\", "/") == f"inputs/{unit}/handoff.json"]
        if own and payload.get("catalog_handoff_sha256") and own[0]["sha256"] != payload["catalog_handoff_sha256"]:
            report.add("error", "T-HANDOFF", unit, f"{payload_path.name} names catalog_handoff_sha256 "
                       f"{payload['catalog_handoff_sha256']}, but the handoff its record binds ({own[0]['path']}) is "
                       f"{own[0]['sha256']}")

    # The wrapper that wrote the record, at its commit: its acceptance, context and redaction rules made the record's
    # statements about the session.
    wrapper = record["wrapper"]
    if wrapper["repo_commit"] is None:
        report.add("hold", "T-RECORD", unit, f"{record_path.name}: the wrapper {wrapper['path']} that wrote it was not a "
                   "commit's (an edited working tree), so the rules behind its tool_calls and context cannot be identified")
    else:
        wrapper_bytes, why = provenance.code.read(wrapper["repo_commit"], wrapper["path"])
        if wrapper_bytes is None:
            report.add("hold", "T-RECORD", unit, f"{record_path.name}: the wrapper cannot be read at its commit ({why})")
        elif not provenance.code_matches(wrapper_bytes, wrapper["sha256"]):
            report.add("error", "T-RECORD", unit, f"{record_path.name}: {wrapper['path']} at {wrapper['repo_commit'][:12]} "
                       f"hashes to {sha256_bytes(wrapper_bytes)}; the record names {wrapper['sha256']}")

    # The skill at its commit: its bytes, its version and the tools it allows.
    skill = record["skill"]
    allowed: list[str] | None = None
    if skill["name"] != SKILL_NAME:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: skill {skill['name']!r} is not {SKILL_NAME}")
    skill_bytes, why = provenance.code.read(skill["repo_commit"], skill["path"])
    if skill_bytes is None:
        report.add("hold", "T-RECORD", unit, f"{record_path.name}: SKILL.md cannot be read at repo_commit ({why}); the "
                   "skill and the tools it allows were not checked")
    else:
        text = skill_bytes.decode("utf-8", "replace")
        if not provenance.code_matches(skill_bytes, skill["sha256"]):
            report.add("error", "T-RECORD", unit, f"{record_path.name}: {skill['path']} at {skill['repo_commit'][:12]} hashes "
                       f"to {sha256_bytes(skill_bytes)}; the record names {skill['sha256']}")
        version = skill_version(text)
        if version != skill["version"]:
            report.add("error", "T-RECORD", unit, f"{record_path.name}: the skill at its commit is version {version}, the "
                       f"record names {skill['version']}")
        written_by = (payload.get("decided_by") or {}).get("skill_version")
        if v2 and written_by != skill["version"]:
            report.add("error", "T-RECORD", unit, f"{record_path.name}: the payload was written by skill version "
                       f"{written_by}, the record names {skill['version']}")
        allowed = skill_allowed_tools(text)

    # The prompt: the template at the commit, the rendered prompt beside the record.
    prompt = record["prompt"]
    template, why = provenance.code.read(skill["repo_commit"], prompt["template_path"])
    if template is None:
        report.add("hold", "T-RECORD", unit, f"{record_path.name}: the prompt template cannot be read at repo_commit ({why})")
    elif not provenance.code_matches(template, prompt["template_sha256"]):
        report.add("error", "T-RECORD", unit, f"{record_path.name}: {prompt['template_path']} at its commit hashes to "
                   f"{sha256_bytes(template)}; the record names {prompt['template_sha256']}")
    _file_check(report, unit, record_path, "the rendered prompt", prompt["rendered_path"], prompt["rendered_sha256"], None,
                bundle)

    # The tool calls: in order, each allowed by the skill, each result as stored.
    calls = record["tool_calls"]
    for index, call in enumerate(calls, start=1):
        if call["seq"] != index:
            report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool_calls[{index - 1}] has seq {call['seq']}; the "
                       "calls are numbered 1, 2, ... in the order the transcript shows them")
        stored = None
        has_result = call["result_path"] is not None or call["result_sha256"] is not None or call["status"] == "ok"
        if has_result:
            stored = _file_check(report, unit, record_path, f"the result of tool call {call['seq']}", call["result_path"],
                                 call["result_sha256"], None, bundle, check="T-TOOLS")
        else:
            report.add("hold", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} ({call['tool']}) has no "
                       "stored result; what it returned to the agent cannot be checked")
        # A call to a tool the skill does not allow is an error, unless its stored result is the harness's refusal
        # (no such tool): then it never ran. The transcript comparison below checks that the harness did not offer it.
        if allowed is not None and not tool_allowed(call["tool"], allowed):
            if call["status"] == "error" and harness_refused(call["tool"], _stored_result(stored)):
                report.add("note", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} asked for {call['tool']}, "
                           "which the skill does not allow; the harness refused it (no such tool), so it never ran")
            else:
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} uses {call['tool']}, "
                           f"which the skill does not allow ({', '.join(allowed)})")
    if allowed is None and calls and skill_bytes is not None:
        report.add("hold", "T-TOOLS", unit, f"{record_path.name}: the skill at its commit lists no allowed-tools, so its "
                   f"{len(calls)} tool call(s) cannot be checked against it")

    # The released redacted transcript: the harness's own account, which tool_calls, the model and the session must
    # match. Without it they are the wrapper's statements, which nobody else can check, so the decision holds.
    transcript = record["transcript"] or {}
    released = transcript.get("released_path")
    if not released or not transcript.get("sha256"):
        report.add("hold", "T-TRANSCRIPT", unit, f"{record_path.name} names no released transcript; its tool_calls, model "
                   "and session are the wrapper's statements, not checked against the harness's own record")
    else:
        candidates = [record_path.parent / released, Path(triage_dir).parent.parent / released]
        if bundle is not None:
            candidates.append(bundle.root / released)
        found = None if _ABSOLUTE.match(str(released)) else next((p for p in candidates if p.is_file()), None)
        if found is None:
            report.add("hold", "T-TRANSCRIPT", unit, f"{record_path.name}: the redacted transcript {released} is not here; "
                       "its sha256 and the tool calls it shows were not checked")
        else:
            data = found.read_bytes()
            if sha256_bytes(data) != str(transcript.get("sha256") or ""):
                report.add("error", "T-RECORD", unit, f"{record_path.name}: the released transcript {released} hashes to "
                           f"{sha256_bytes(data)}; the record names {transcript.get('sha256')}")
            else:
                compare_transcript(report, unit, record_path, record, data, allowed)

    # The derivation: a payload expanded from the agent's spec is that spec's deterministic image (T-SPEC), over a digest
    # that is the digest script's image of the inputs (T-DIGEST).
    if record.get("derivation") is not None:
        check_derivation(report, unit, record_path, record, payload_bytes, provenance)
    elif isinstance(payload.get("derivation"), dict):
        report.add("error", "T-SPEC", unit, f"{payload_path.name} says it was expanded from {payload['derivation'].get('spec')}, "
                   f"but its record {record_path.name} binds no derivation; nothing ties it to that spec")
    elif v2:
        # From skill 1.2.0 the agent writes a spec and code writes the payloads; in an accession whose payloads are
        # expanded from a spec, none is written another way. A payload without a derivation there was not made by the
        # expander, whatever its record says.
        why = []
        if _version(skill["version"]) >= SPEC_SKILL_VERSION:
            why.append(f"its record names skill {skill['version']}, which writes a spec and never a payload")
        if provenance.derived_accession:
            why.append(provenance.derived_accession)
        if why:
            report.add("error", "T-SPEC", unit, f"{payload_path.name}'s record {record_path.name} binds no derivation, but "
                       f"{'; and '.join(why)}; a payload that is not its spec's expansion is not accepted")
    if v2 and (record.get("derivation") is not None or _version(skill["version"]) >= SPEC_SKILL_VERSION):
        own = re.compile(rf"^{re.escape(unit)}(?:\.r[0-9]+)?\.json$", re.IGNORECASE)
        for call in record["tool_calls"]:
            target = str((call.get("arguments") or {}).get("file_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if bare_tool(str(call["tool"])) in ("Write", "Edit", "MultiEdit") and own.match(target):
                report.add("error", "T-SPEC", unit, f"{record_path.name}: tool call {call['seq']} ({bare_tool(str(call['tool']))}) "
                           f"writes {target} itself; a spec session writes the spec, and the payloads are the expander's")

    # The context: clean, or a hold.
    context = record["agent"]["context"]
    if context["configuration"] != "clean":
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: the agent ran in configuration "
                   f"{context['configuration']!r}, not clean")
    if context["context_files"]:
        shown_files = ", ".join(item["path"] for item in context["context_files"][:3])
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: the harness loaded {len(context['context_files'])} "
                   f"context file(s) ({shown_files}); a clean run loads none")
        for item in context["context_files"]:
            location = record_path.parent / item["path"]
            if not _ABSOLUTE.match(item["path"]) and location.is_file() and sha256_bytes(location.read_bytes()) != item["sha256"]:
                report.add("error", "T-RECORD", unit, f"{record_path.name}: context file {item['path']} hashes to "
                           f"{sha256_bytes(location.read_bytes())}; the record names {item['sha256']}")
    if context["memory"] != "disabled":
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: memory was {context['memory']!r}, not disabled")
    if context["hooks"]:
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: the harness ran {len(context['hooks'])} hook(s)")
    if allowed is not None:
        servers = {tool_server(item.split("(", 1)[0].strip()) for item in allowed} - {None}
        extra = sorted({item["name"] for item in context["mcp_servers"]} - servers)
        if extra:
            report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: MCP server(s) {', '.join(extra)} were loaded, which "
                       "the skill does not allow")
    called = sorted({tool_server(str(call["tool"]).split("(", 1)[0].strip()) for call in calls} - {None})
    unlisted = [name for name in called if name not in {item["name"] for item in context["mcp_servers"]}]
    if unlisted:
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: tool calls use MCP server(s) {', '.join(unlisted)}, "
                   "which agent.context.mcp_servers does not list; the servers the session ran with are not stated")
    flags = list(context["harness_flags"])
    if not flags:
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: agent.context.harness_flags is empty; how the session "
                   "was made clean is not stated")
    elif record["agent"]["harness"] == "claude-code":
        missing = [flag for flag in CLAUDE_CODE_CLEAN_FLAGS if flag not in flags]
        if missing:
            report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: the claude-code session's flags lack "
                       f"{', '.join(missing)}, which a clean configuration sets")
    return record


_MATERIALISED: list[Path] = []


def _cleanup_materialised() -> None:
    for folder in _MATERIALISED:
        shutil.rmtree(folder, ignore_errors=True)


atexit.register(_cleanup_materialised)


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def load_code_module(provenance: Provenance, code_file: dict[str, Any]) -> tuple[Any, str | None, str]:
    """The script a record names (derivation.expander or derivation.digest.script: path, sha256, repo_commit and tree, the
    sha256 of each file it reads beside itself, SPEC_CODE_TREE), read at its commit (git show, or the --code-root) and
    loaded as a module with those files, or (None, why, level). Every call checks the record's own hashes, the script's
    and the tree's, against the bytes read: a hash that is not the file's is an error (level "error"); code that cannot
    be read or loaded is a hold. The running tree's own files are used when every byte is the commit's (line endings
    aside); otherwise the files are written to a scratch folder and loaded from there, so a record is re-derived by the
    code it names, never by whatever runs today. A loaded module is cached by the bytes of its files."""
    commit, path, expected_sha256 = code_file["repo_commit"], code_file["path"], code_file["sha256"]
    main, why = provenance.code.read(commit, path)
    if main is None:
        return None, f"{path} cannot be read at {str(commit)[:12]} ({why})", "hold"
    if not provenance.code_matches(main, expected_sha256):
        return None, (f"{path} at {str(commit)[:12]} hashes to {sha256_bytes(main)}; the record names "
                      f"{expected_sha256}"), "error"
    named = code_file.get("tree")
    if not isinstance(named, dict) or set(named) != set(SPEC_CODE_TREE):
        return None, (f"the record binds {path} without the sha256 of every file it reads beside itself "
                      f"({', '.join(SPEC_CODE_TREE)}); the helpers and schemas behind the re-derivation are not bound"), "error"
    tree: dict[str, bytes] = {path: main}
    for relative in SPEC_CODE_TREE:
        data, why = provenance.code.read(commit, relative)
        if data is None:
            return None, f"{relative} cannot be read at {str(commit)[:12]} ({why})", "hold"
        if not provenance.code_matches(data, str(named[relative])):
            return None, (f"{relative} at {str(commit)[:12]} hashes to {sha256_bytes(data)}; the record's tree names "
                          f"{named[relative]}"), "error"
        tree[relative] = data
    key = sha256_bytes(canonical_json({rel: sha256_bytes(_lf(data)) for rel, data in sorted(tree.items())}) + path.encode("utf-8"))
    if key in provenance.modules:
        module, why = provenance.modules[key]
        return module, why, "hold"
    here = all((REPO_ROOT / rel).is_file() and _lf((REPO_ROOT / rel).read_bytes()) == _lf(data) for rel, data in tree.items())
    if here:
        root = REPO_ROOT
    else:
        root = Path(tempfile.mkdtemp(prefix="triage-code-"))
        _MATERIALISED.append(root)
        for relative, data in tree.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    name = f"triage_code_{hashlib.sha256((str(root) + path).encode('utf-8')).hexdigest()[:16]}"
    try:
        spec = importlib.util.spec_from_file_location(name, root / path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001 - code from another commit may fail in any way; that is a hold, not a crash
        provenance.modules[key] = (None, f"{path} at {str(commit)[:12]} cannot be loaded ({type(error).__name__}: {error})")
        return None, provenance.modules[key][1], "hold"
    provenance.modules[key] = (module, None)
    return module, None, "hold"


def _bound_input_files(record_path: Path, record: dict[str, Any], bundle: "ReleaseBundle | None") -> tuple[dict[str, bytes], dict[str, str]]:
    """({path under inputs/: bytes}, {path: original sha256 for a file released as a projection}) of the record's inputs.
    Only files whose bytes are the ones the record names are taken; the record's own checks report the others."""
    files: dict[str, bytes] = {}
    originals: dict[str, str] = {}
    for item in record["inputs"]:
        text = str(item.get("path") or "").replace("\\", "/")
        if not text.startswith("inputs/") or _ABSOLUTE.match(text):
            continue
        location = record_path.parent / text
        try:
            data = location.read_bytes()
        except OSError:
            continue
        entry = bundle.projected(location) if bundle is not None else None
        relative = text[len("inputs/"):]
        if entry is not None:
            if sha256_bytes(data) == entry["release_sha256"]:
                files[relative] = data
                originals[relative] = entry["original_sha256"]
        elif sha256_bytes(data) == str(item.get("sha256") or ""):
            files[relative] = data
    return files, originals


def check_derivation(report: "Report", unit: str, record_path: Path, record: dict[str, Any], payload_bytes: bytes,
                     provenance: Provenance) -> None:
    """T-SPEC and T-DIGEST: the payload is the expansion, by the expander at its commit, of the spec the record binds over
    the inputs it binds and the context it states; the digest is the digest script's image of those inputs."""
    derivation = record["derivation"]
    bundle = provenance.bundle
    spec_info = derivation["spec"]
    spec_bytes = _file_check(report, unit, record_path, "the spec", spec_info["path"], spec_info["sha256"], spec_info["bytes"],
                             bundle, check="T-SPEC")
    context = derivation["context"]
    for field_name, expected, label in (("decided_at", record["finished_at"], "the record's finished_at"),
                                        ("model", record["agent"]["model_id"], "the record's model_id"),
                                        ("skill_version", record["skill"]["version"], "the record's skill version")):
        if context[field_name] != expected:
            report.add("error", "T-SPEC", unit, f"{record_path.name}: derivation.context.{field_name} {context[field_name]!r} is "
                       f"not {label} {expected!r}")
    files, originals = _bound_input_files(record_path, record, bundle)

    digest = derivation.get("digest")
    if digest is not None:
        digest_bytes = _file_check(report, unit, record_path, "the digest", digest["path"], digest["sha256"], None, bundle,
                                   check="T-DIGEST")
        script = digest["script"]
        module, why, level = load_code_module(provenance, script)
        if module is None:
            report.add(level, "T-DIGEST", unit, f"{record_path.name}: the digest cannot be rebuilt ({why})")
        elif str(getattr(module, "DIGEST_VERSION", "")) != script["version"]:
            report.add("error", "T-DIGEST", unit, f"{record_path.name}: the digest script at its commit is version "
                       f"{getattr(module, 'DIGEST_VERSION', None)}, the record names {script['version']}")
        elif digest_bytes is not None:
            try:
                rebuilt = module.build_digest_from_files(files)
            except Exception as error:  # noqa: BLE001
                report.add("error", "T-DIGEST", unit, f"{record_path.name}: the digest script fails on the bound inputs "
                           f"({type(error).__name__}: {error})")
            else:
                if _lf(rebuilt) != _lf(digest_bytes):
                    report.add("error", "T-DIGEST", unit, f"{record_path.name}: {digest['path']} is not what the digest script "
                               "at its commit builds from the inputs the record binds")

    expander = derivation["expander"]
    if spec_bytes is None:
        return
    module, why, level = load_code_module(provenance, expander)
    if module is None:
        report.add(level, "T-SPEC", unit, f"{record_path.name}: the payload cannot be re-derived from its spec ({why})")
        return
    if str(getattr(module, "EXPANDER_VERSION", "")) != expander["version"]:
        report.add("error", "T-SPEC", unit, f"{record_path.name}: the expander at its commit is version "
                   f"{getattr(module, 'EXPANDER_VERSION', None)}, the record names {expander['version']}")
        return
    key = sha256_bytes(canonical_json({"spec": sha256_bytes(spec_bytes), "name": Path(spec_info["path"]).name,
                                       "expander": [expander["repo_commit"], expander["sha256"]], "context": context,
                                       "files": {k: sha256_bytes(v) for k, v in sorted(files.items())}, "originals": originals}))
    if key not in provenance.expansions:
        try:
            inputs = module.inputs_from_files(files)
            for relative, original in originals.items():
                parts = relative.split("/")
                if len(parts) == 2 and parts[1] == "handoff.json" and parts[0] in inputs.units:
                    inputs.units[parts[0]].handoff_sha256 = original
            ctx = module.ExpansionContext(decided_at=context["decided_at"], model=context["model"],
                                          skill_version=context["skill_version"], agent=context["agent"],
                                          purpose=context["purpose"])
            provenance.expansions[key] = module.expand(spec_bytes, inputs, ctx, spec_name=Path(spec_info["path"]).name)
        except Exception as error:  # noqa: BLE001
            provenance.expansions[key] = error
    expansion = provenance.expansions[key]
    if isinstance(expansion, Exception):
        report.add("error", "T-SPEC", unit, f"{record_path.name}: the expander fails on the spec and the bound inputs "
                   f"({type(expansion).__name__}: {expansion})")
        return
    if expansion.findings:
        shown = "; ".join(f"{f.code} {f.unit or '-'}: {f.message[:160]}" for f in expansion.findings[:3])
        report.add("error", "T-SPEC", unit, f"{record_path.name}: the spec does not expand over the bound inputs "
                   f"({len(expansion.findings)} finding(s): {shown})")
        return
    check_revision_scope(report, unit, record_path, record, spec_bytes, expansion, module, provenance)
    expanded = expansion.payloads.get(unit)
    if expanded is None:
        report.add("error", "T-SPEC", unit, f"{record_path.name}: the spec expands to no payload for unit {unit}")
    elif expanded != payload_bytes:
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
            spec = json.loads(spec_bytes.decode("utf-8-sig"))
            ids = {g.get("id") for g in spec.get("groups") or []} | {e.get("id") for e in spec.get("exceptions") or []}
            stray = sorted({str(i.get("rule_id")) for key_ in ("rows", "added_members", "excluded_rows", "excluded_members")
                            for i in payload.get(key_) or [] if i.get("rule_id") not in ids})
        except (UnicodeDecodeError, ValueError, AttributeError):
            stray = []
        report.add("error", "T-SPEC", unit, f"{record_path.name}: the payload is not the spec's expansion byte for byte"
                   + (f"; rule_id(s) {', '.join(stray[:5])} are not in the spec" if stray else "")
                   + "; a payload is written by the expander, never by hand")


def spec_number(name: str) -> int | None:
    """The revision number of a spec file name: 0 for <accession>.spec.json, N for .spec.r<N>.json, None otherwise."""
    match = SPEC_FILE.match(Path(str(name)).name)
    return None if match is None else int(match.group("n") or 0)


def check_revision_scope(report: "Report", unit: str, record_path: Path, record: dict[str, Any], spec_bytes: bytes,
                         expansion: Any, module: Any, provenance: Provenance) -> None:
    """X-SCOPE, re-checked (T-SPEC): a revision spec (<accession>.spec.r<N>.json) expands every unit it does not revise
    to that unit's payload as it stood before the revision (its latest payload expanded from an earlier spec), decision
    fields aside. The units it revises are those whose records bind the same spec."""
    number = spec_number(record["derivation"]["spec"]["path"])
    if not number:
        return
    revised = provenance.spec_units.get(sha256_bytes(spec_bytes), set()) | {unit}
    for other, data in sorted(expansion.payloads.items()):
        if other in revised:
            continue
        earlier = [(n, doc) for n, doc in provenance.derived.get(other, []) if n < number]
        if not earlier:
            report.add("hold", "T-SPEC", unit, f"{record_path.name}: the revision spec also expands {other}, which has no "
                       "payload expanded from an earlier spec; that the revision leaves it as it was is not checked")
            continue
        before = max(earlier, key=lambda item: item[0])[1]
        if module.comparable(json.loads(data.decode("utf-8"))) != module.comparable(before):
            report.add("error", "T-SPEC", unit, f"{record_path.name}: the revision spec changes {other}, which it does not "
                       f"revise (X-SCOPE): its expansion of {other} is not {other}'s payload before the revision")


def compare_transcript(report: "Report", unit: str, record_path: Path, record: dict[str, Any], data: bytes,
                       allowed: list[str] | None) -> None:
    """The record against the released transcript: every tool call in order with its server-qualified name, its
    arguments and its stored result; every call the transcript shows on the skill's allowed list; the model and the
    session the transcript names."""
    shown = transcript_events(data)
    calls = record["tool_calls"]
    names = [item["name"] for item in shown["calls"]]
    listed = [str(call["tool"]) for call in calls]
    if allowed is not None:
        offered = {str(t) for t in ((shown["init"] or {}).get("tools") or [])}
        for name in sorted(set(names)):
            if tool_allowed(name, allowed):
                continue
            attempts = [item for item in shown["calls"] if item["name"] == name]
            if name not in offered and attempts and all(
                    item["result"] is not None and item["result"].get("is_error")
                    and harness_refused(name, item["result"]) for item in attempts):
                report.add("note", "T-TOOLS", unit, f"{record_path.name}: the released transcript shows {len(attempts)} "
                           f"call(s) to {name}, which the harness did not offer and refused (no such tool); none ran")
            else:
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: the released transcript shows a call to {name}, "
                           f"which the skill does not allow")
    if names != listed:
        report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool_calls ({len(listed)}: "
                   f"{', '.join(bare_tool(n) for n in listed[:8]) or 'none'}) differ from the {len(names)} tool call(s) the "
                   f"released transcript shows ({', '.join(bare_tool(n) for n in names[:8]) or 'none'})")
    else:
        for call, seen in zip(calls, shown["calls"]):
            if canonical_json(call["arguments"]) != canonical_json(seen["input"]):
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: the arguments of tool call {call['seq']} are not "
                           "the ones the released transcript shows")
            result = seen["result"]
            if result is None:
                if call["result_path"] is not None:
                    report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} has a stored result, "
                               "but the released transcript shows none")
                continue
            stored = None
            if call["result_path"] and not _ABSOLUTE.match(str(call["result_path"])):
                try:
                    stored = json.loads((record_path.parent / call["result_path"]).read_bytes().decode("utf-8-sig"))
                except (OSError, UnicodeDecodeError, ValueError):
                    stored = None
            if not isinstance(stored, dict) or "content" not in stored:
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: the stored result of tool call {call['seq']} is "
                           "not readable as {tool_use_id, tool, is_error, content}")
            elif (canonical_json(stored.get("content")) != canonical_json(result.get("content"))
                  or bool(stored.get("is_error")) != bool(result.get("is_error"))
                  or stored.get("tool_use_id") != seen["id"]):
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: the stored result of tool call {call['seq']} is "
                           "not the result the released transcript shows")
            elif bool(stored.get("is_error")) != (call["status"] == "error"):
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} is {call['status']}, its "
                           "result says otherwise")
    agent = record["agent"]
    if agent["model_id"] == "unknown" or not shown["models"]:
        report.add("hold", "T-RECORD", unit, f"{record_path.name}: the model is not known (the record names "
                   f"{agent['model_id']!r}, the transcript {', '.join(shown['models']) or 'none'})")
    elif agent["model_id"] not in shown["models"]:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: model_id {agent['model_id']!r} is not a model the "
                   f"released transcript names ({', '.join(shown['models'])})")
    init_session = (shown["init"] or {}).get("session_id")
    if init_session and str(init_session) != agent["session_id"]:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: session_id {agent['session_id']!r} is not the session "
                   "the released transcript names")


# ------------------------------------------------------------------------------ the review log (section 2.4)


def read_review_log(triage_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """(the entries of the accession's review-log.jsonl, why its hash chain does not hold). No log is no entry."""
    path = Path(triage_dir) / REVIEW_LOG
    if not path.is_file():
        return [], []
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    previous = None
    for index, line in enumerate([item for item in path.read_bytes().split(b"\n") if item.strip()], start=1):
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"line {index} does not parse")
            entry = None
        if isinstance(entry, dict):
            if entry.get("seq") != index:
                problems.append(f"line {index} has seq {entry.get('seq')}")
            if entry.get("prev_sha256") != previous:
                problems.append(f"line {index} does not chain to line {index - 1}")
            entries.append(entry)
        elif entry is not None:
            problems.append(f"line {index} is not an object")
        previous = sha256_bytes(line)
    return entries, problems


def _revision_entries(log: list[dict[str, Any]], decision_id: str) -> list[dict[str, Any]]:
    return [e for e in log if e.get("kind") == "revision" and e.get("new_decision_id") == decision_id]


def lineage(decision_id: str, supersedes: dict[str, str | None], log: list[dict[str, Any]]) -> set[str]:
    """This decision and every one it supersedes, by the records' supersedes and the review log's revision entries."""
    found = {decision_id}
    queue = [decision_id]
    while queue:
        current = queue.pop()
        earlier = {supersedes.get(current)} | {e.get("supersedes") for e in _revision_entries(log, current)}
        for item in earlier:
            if isinstance(item, str) and item and item not in found:
                found.add(item)
                queue.append(item)
    return found


def answered_codes(decision_id: str, payload: dict[str, Any], supersedes: dict[str, str | None],
                   log: list[dict[str, Any]]) -> set[str]:
    """The question codes a named person answered for this payload or one it supersedes (review-log person_answer
    entries); a v1 payload's own person_answers count as well."""
    ids = lineage(decision_id, supersedes, log)
    codes = {str(e.get("question_code")) for e in log
             if e.get("kind") == "person_answer" and e.get("decision_id") in ids and str(e.get("by") or "").strip()
             and str(e.get("question_code") or "").strip()}
    if payload.get("schema") != SCHEMA_V2_ID:
        codes |= {str(a.get("code")) for a in payload.get("person_answers", []) if str(a.get("by") or "").strip()}
    return codes


def _blocking(payload: dict[str, Any] | None) -> set[str]:
    return {f.get("code") for f in (payload or {}).get("flags", []) if isinstance(f, dict)} & blocking_flags_for(payload)


def _codes(payload: dict[str, Any] | None) -> set[str]:
    return {f.get("code") for f in (payload or {}).get("flags", []) if isinstance(f, dict)}


def check_revision(report: "Report", unit: str, path: Path, decision_id: str, payload: dict[str, Any],
                   payloads: dict[str, tuple[Path, dict[str, Any] | None]], supersedes: dict[str, str | None],
                   log: list[dict[str, Any]], unit_files: list[tuple[int, Path]]) -> None:
    """A v2 revision may drop a blocking flag of the payload it supersedes only on a person's answer: the cause of its
    revision entry must be a person_answer with that flag's question_code, for this decision or one it supersedes."""
    earlier = {supersedes.get(decision_id)} | {e.get("supersedes") for e in _revision_entries(log, decision_id)}
    earlier = {item for item in earlier if isinstance(item, str) and item}
    if not earlier:
        # No record or log entry names what it revises: the revision before it by name (<unit>.r<N-1>.json, ...).
        match = _REVISION.match(path.stem)
        number = int(match.group("n")) if match else 0
        lower = [(n, p) for n, p in unit_files if n < number]
        if not lower:
            return
        before = max(lower)[1]
        earlier = {sha for sha, (p, _) in payloads.items() if p == before}
    ids = lineage(decision_id, supersedes, log)
    by_seq = {e.get("seq"): e for e in log}
    cited = set()
    for entry in _revision_entries(log, decision_id):
        cause = entry.get("cause") if isinstance(entry.get("cause"), dict) else {}
        answer = by_seq.get(cause.get("seq"))
        if (cause.get("kind") == "person_answer" and answer is not None and answer.get("kind") == "person_answer"
                and answer.get("decision_id") in ids and str(answer.get("by") or "").strip()):
            cited.add(str(answer.get("question_code")))
    for sha in sorted(earlier):
        if sha not in payloads:
            report.add("hold", "T-REVISION", unit, f"{path.name} supersedes {sha}, which is not in this directory; the flags "
                       "it dropped cannot be checked")
            continue
        # A flag the revision still carries is not dropped, even where a later skill no longer counts it as blocking.
        dropped = _blocking(payloads[sha][1]) - _codes(payload)
        for code in sorted(dropped - cited):
            report.add("error", "T-REVISION", unit, f"{path.name} drops the blocking flag {code} that "
                       f"{payloads[sha][0].name} carried, but no revision entry of the review log cites a person's answer "
                       f"to {code} as its cause; a flag is cleared by a person, not by a revision")


def _payload_files(triage_dir: Path) -> dict[str, list[tuple[int, Path]]]:
    """Every payload of the directory, current or superseded, by unit: [(revision number, path)], 0 for <unit>.json."""
    out: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for path in sorted(triage_dir.glob("*.json")):
        if not is_payload_file(path.name):
            continue
        match = _REVISION.match(path.stem)
        unit, number = (match.group("unit"), int(match.group("n"))) if match else (path.stem, 0)
        out[unit].append((number, path))
    return {unit: sorted(items) for unit, items in out.items()}


def _version(text: Any) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return ()


# The first skill version whose agent writes a spec and never a payload (docs/llm-assisted-curation.md section 2.2).
SPEC_SKILL_VERSION = (1, 2, 0)


def _derivation_context(triage_dir: Path, all_files: dict[str, list[tuple[int, Path]]], provenance: Provenance) -> None:
    """What the accession's files say about derivation (T-SPEC): whether a spec sits in the directory or a record binds
    one (then no v2 payload of it may lack a derivation), every unit's payloads expanded from a spec by revision number
    (X-SCOPE), and the units each spec's records name."""
    specs = sorted(p.name for p in triage_dir.glob("*.json") if is_spec_file(p.name))
    binding: list[str] = []
    for unit, items in all_files.items():
        for _number, path in items:
            try:
                record = json.loads(path.with_name(path.name[: -len(".json")] + RECORD_SUFFIX).read_bytes().decode("utf-8-sig"))
                payload = json.loads(path.read_bytes().decode("utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            derivation = record.get("derivation") if isinstance(record, dict) else None
            if not isinstance(derivation, dict) or not isinstance(derivation.get("spec"), dict):
                continue
            binding.append(record.get("payload", {}).get("path") or path.name)
            number = spec_number(derivation["spec"].get("path") or "")
            if number is not None and isinstance(payload, dict):
                provenance.derived.setdefault(unit, []).append((number, payload))
            provenance.spec_units.setdefault(str(derivation["spec"].get("sha256")), set()).add(unit)
    reasons = []
    if specs:
        reasons.append(f"the directory holds the spec {specs[0]}")
    if binding:
        reasons.append(f"the record of {sorted(binding)[0]} binds a spec")
    provenance.derived_accession = ", and ".join(reasons) or None


def validate(
    triage_dir: Path,
    catalog: dict[str, CatalogUnit] | None = None,
    listings: Iterable[ListedMember] = (),
    *,
    code_root: Path | None = None,
    bundle: ReleaseBundle | None = None,
    listing_files: Iterable[Path] | None = None,
    payload_only: bool = False,
    polarity_split: Iterable[Path] = (),
) -> Report:
    """The accession's triage files as a set, with their decision records and review log.

    `code_root` is the released code (SKILL.md, the prompt templates) read in place of git at each record's repo_commit;
    `bundle` the release bundle the directory sits in (found on its own when <triage dir>/../../release-index.json
    exists), through whose index projected inputs are checked. `listing_files` are the files `listings` were read from
    (the CLI's --archive-listing): each record's archive_listing inputs must be among them, and each of them bound by a
    record (T-RECORD, T-DUP); without them that tie is not checked. `payload_only` checks the payloads alone (the in-session
    spec check, scripts/triage-spec-server.py): no decision record, transcript, context or revision check runs. `polarity_split` names polarity-split children (the CLI's
    --polarity-split; POLARITY SPLIT above), each checked in place of its parent's payload once the parent's own file,
    record and revision are checked."""
    report = Report()
    triage_dir = Path(triage_dir)
    if bundle is None:
        bundle = ReleaseBundle.find(triage_dir)
    given = None if listing_files is None else {sha256_bytes(Path(p).read_bytes()): Path(p) for p in listing_files}
    provenance = Provenance(CodeSource(code_root), bundle, None if given is None else set(given))
    triages: dict[str, dict[str, Any]] = {}
    files = current_triage_files(triage_dir)
    if not files:
        report.add("error", "T-SET", None, f"no triage files in {triage_dir}")
        return report

    # Every payload of the directory, superseded ones included, by sha256, with what its record says it supersedes.
    all_files = _payload_files(triage_dir)
    payloads: dict[str, tuple[Path, dict[str, Any] | None]] = {}
    supersedes: dict[str, str | None] = {}
    for items in all_files.values():
        for _number, path in items:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            sha = sha256_bytes(raw)
            try:
                value = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                value = None
            payloads[sha] = (path, value if isinstance(value, dict) else None)
            try:
                named = json.loads(path.with_name(path.name[: -len(".json")] + RECORD_SUFFIX).read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError):
                named = None
            if isinstance(named, dict) and isinstance(named.get("supersedes"), str):
                supersedes[sha] = named["supersedes"]
    if not payload_only:
        _derivation_context(triage_dir, all_files, provenance)
    split_children: dict[str, tuple[Path, Any]] = {}
    for child_path in polarity_split:
        child_path = Path(child_path)
        try:
            child_data = json.loads(child_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            report.add("error", "T-SPLIT", None, f"the polarity-split child {child_path.name} is not readable JSON: {exc}")
            continue
        child_unit = str(child_data.get("unit_id") or "") if isinstance(child_data, dict) else ""
        if child_unit in split_children:
            report.add("error", "T-SPLIT", child_unit or None, "two polarity-split children of one unit in one check; "
                       "each polarity is checked on its own")
            continue
        split_children[child_unit] = (child_path, child_data)
    log, log_problems = read_review_log(triage_dir)
    for problem in log_problems:
        report.add("error", "T-LOG", None, f"{REVIEW_LOG}: {problem}; the answers and revisions it records cannot be trusted")
    answered: dict[str, set[str]] = {}

    for path in files:
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            report.add("error", "T-SCHEMA", path.stem, f"{path.name} is not readable JSON: {exc}")
            continue
        unit = data.get("unit_id") if isinstance(data, dict) else None
        unit = unit if isinstance(unit, str) and unit else triage_file_unit(path)
        if isinstance(data, dict) and "polarity_split" in data:
            report.add("error", "T-SPLIT", unit, f"{path.name} is a polarity-split child; a child is checked with "
                       "--polarity-split beside its parent's payload, never as a triage file of its own")
            continue
        if unit in triages:
            report.add("error", "T-SET", unit, f"unit {unit} is triaged twice")
            continue
        report.units.append(unit)
        decision_id = sha256_bytes(raw)
        schema_id = data.get("schema") if isinstance(data, dict) else None
        report.payloads[unit] = {"payload": path.name, "schema": schema_id, "decision_id": decision_id}
        if schema_id not in SCHEMA_PATHS:
            report.add("error", "T-SCHEMA", unit, f"{path.name}: schema {schema_id!r} is not one of {sorted(SCHEMA_PATHS)}")
            continue
        schema = load_schema(schema_id)
        problems = schema_errors(data, schema, schema)
        if problems:
            for problem in problems[:20]:
                report.add("error", "T-SCHEMA", unit, f"{path.name}: {problem}")
            if len(problems) > 20:
                report.add("error", "T-SCHEMA", unit, f"{path.name}: {len(problems) - 20} more schema problem(s)")
            continue
        if triage_file_unit(path) != unit:
            report.add("error", "T-SET", unit, f"{path.name} holds unit {unit}; name the file {unit}.json (or {unit}.r<N>.json)")
        triages[unit] = data
        report.unit_status[unit] = data["status"]
        for citation in class_citations(data) + ([] if payload_only else record_class_citations(path)):
            report.add("error", "T-CLASS", unit, f"{path.name}: {citation}; the triage binds the Catalog's rows, files and "
                       "listing and never the Class, which runs after it and binds through its own ratification "
                       "(WS0 decision F)")
        if schema_id == SCHEMA_ID:
            report.add("note", "T-RATIONALE", unit, f"rationale_not_recorded: {path.name} is {SCHEMA_ID}, which carries no "
                       "confidence or rationale")
        if not payload_only:
            check_record(report, unit, triage_dir, path, raw, data, provenance)
        answered[unit] = answered_codes(decision_id, data, supersedes, log)
        if schema_id == SCHEMA_V2_ID and not payload_only:
            check_revision(report, unit, path, decision_id, data, payloads, supersedes, log, all_files.get(unit, []))
        if unit in split_children:
            child_path, child_data = split_children.pop(unit)
            record = path.with_name(path.name[: -len(".json")] + RECORD_SUFFIX)
            record_sha = sha256_bytes(record.read_bytes()) if record.is_file() else None
            if check_polarity_split(report, unit, data, decision_id, f"{triage_dir.name}/{path.name}", record_sha, child_data,
                                    accession_payloads(triage_dir)):
                child_problems = schema_errors(child_data, schema, schema)
                for problem in child_problems[:20]:
                    report.add("error", "T-SCHEMA", unit, f"{child_path.name}: {problem}")
                if not child_problems:
                    # The child stands for its parent in every content check; the decision answers polarity_switching.
                    triages[unit] = child_data
                    report.unit_status[unit] = child_data["status"]
                    answered[unit] = set(answered[unit]) | {POLARITY_SPLIT_REASON}
                    report.payloads[unit]["polarity_split"] = {
                        "child": child_path.name, "ion_mode": child_data["polarity_split"]["ion_mode"],
                        "decision_id": sha256_bytes(canonical_json(child_data))}

    for child_unit, (child_path, _child_data) in sorted(split_children.items()):
        report.add("error", "T-SPLIT", child_unit or None, f"the polarity-split child {child_path.name} has no parent "
                   "payload in this directory")
    accessions = {t["accession"] for t in triages.values()}
    if len(accessions) > 1:
        report.add("error", "T-SET", None, f"one directory must hold one accession; found {sorted(accessions)}")
    report.accession = sorted(accessions)[0] if accessions else None

    listed = list(listings)
    by_key: dict[str, ListedMember] = {}
    for m in listed:
        by_key.setdefault(member_key(m.path), m)
    resolver = Resolver(m.path for m in listed) if listed else Resolver(_triage_member_paths(triages))

    if catalog is not None:
        # A campaign's handoffs directory spans many accessions; only this accession's units count here.
        catalog = {u: cu for u, cu in catalog.items() if u in triages or (cu.accession is not None and cu.accession == report.accession)}

    for unit, triage in triages.items():
        _check_unit(report, unit, triage, triages, catalog, by_key if listed else None, resolver, answered[unit])

    _check_cross_unit(report, triages, listed, resolver, answered)
    if given is not None and provenance.bound_listings:
        for digest, path in sorted(given.items(), key=lambda item: item[1].name):
            if digest not in provenance.bound_listings:
                report.add("hold", "T-DUP", None, f"the listing {path.name} was given, but no decision record binds it; "
                           "the coverage checks ran on a listing no decision rested on")
    if catalog is not None:
        missing = sorted(set(catalog) - set(triages))
        if missing and report.accession is not None:
            for unit in missing:
                report.add("hold", "T-SET", unit, f"Catalog unit {unit} has no triage file")
    # Checks that could not run hold every unit: what was not checked is not passed.
    for unit in triages:
        if catalog is None:
            report.add("hold", "T-COVER", unit, "no Catalog was given (--catalog): the rows were not checked against the handoff")
        if not listed:
            report.add("hold", "T-DUP", unit, "no archive listing was given (--archive-listing): byte-identical content and members no unit accounts for cannot be ruled out; a person decides whether to go on without one")
    return report


def acquisition_token_problem(name: str, acquisition_mode: str) -> str | None:
    """What a name's acquisition tokens say against a kept file's acquisition_mode (T-TOKEN), or None: a targeted method
    (PRM, SRM, MRM, SIM, tMS2), which the campaign does not run, or an MS1, DDA, DIA, SWATH or AIF token that is not the
    mode the triage gives (DIA, SWATH and AIF count as one family; SCANnn says no mode; unknown and mixed are not
    contradicted); or an MS/MS word (MSMS, MS2, MSe, ...) against MS1."""
    found = token_acquisition(name)
    targeted = [t for t in found if t in TARGETED_TOKENS]
    if targeted:
        return (f"{name} names a targeted method ({', '.join(targeted)}), which the campaign does not run (untargeted DDA or "
                "DIA with an MS1 survey); exclude it as out_of_scope")
    modes = {TOKEN_MODES[t] for t in found if t in TOKEN_MODES}
    mode = "DDA" if acquisition_mode == "targeted_DDA" else acquisition_mode
    if mode == "MS1" and product_ion_words(name):
        return f"{name} says {'+'.join(product_ion_words(name))}, which names product-ion spectra; the triage says MS1"
    if not modes or mode in ("unknown", "mixed") or mode in modes or (mode in DIA_FAMILY and modes & DIA_FAMILY):
        return None
    return f"{name} says {'+'.join(sorted(modes))}, the triage says {acquisition_mode}"


# The reasons a unit excluded as no_product_ion_files may leave its own rows out with: MS1-only (P1), another unit's, not
# in the archive, a byte-identical copy. Anything else (other, no_signal_abstained, out_of_scope_ms1_only, ...) may drop
# a file with product-ion spectra, and then the unit is not without them.
NO_PRODUCT_ION_ROW_REASONS = {"ms1_only_no_msms", "belongs_to_other_unit", "not_in_archive", "duplicate_content"}
# The reasons a member is dropped for good although it may be raw data of some unit: a unit excluded as
# no_product_ion_files or no_files looks at these members before it leaves the campaign.
DROPPED_FOR_GOOD_MEMBER_REASONS = {"other", "no_sample_metadata", "out_of_scope", "requires_conversion", "no_signal_abstained"}


def _flag_names(flag: dict[str, Any], item: dict[str, Any], member_only: bool = False) -> bool:
    """Whether a flag is about an excluded item: it names the item's row or member, or it names none at all (only for a
    flag of the item's own unit: member_only asks for the member to be named)."""
    rows, members = flag.get("row_indices") or [], flag.get("member_paths") or []
    if not rows and not members:
        return not member_only
    if "row_index" in item and item["row_index"] in rows:
        return True
    return bool(item.get("member_path")) and member_key(item["member_path"]) in {member_key(p) for p in members}


def _not_its_arm(path: str, t: dict[str, Any]) -> bool:
    """Whether a name's polarity or chromatography token puts the file in another arm than the unit's."""
    ut = t["unit_technical"]
    polarity, chromatography = token_polarity(path), token_chromatography(path)
    return bool((polarity and ut["ion_mode"] in ("positive", "negative") and polarity != ut["ion_mode"])
                or (chromatography and ut["chromatography"] in ("HILIC", "RP") and chromatography != ut["chromatography"]))


def _members_dropped_for_good(triages: dict[str, dict[str, Any]], resolver: "Resolver") -> dict[str, tuple[str, str]]:
    """{member key: (path, reason)} for the raw-like members any payload excludes for good
    (DROPPED_FOR_GOOD_MEMBER_REASONS) that no unit keeps."""
    kept = {k.key for u, other in triages.items() for k in _kept(u, other, resolver)}
    out: dict[str, tuple[str, str]] = {}
    for other in triages.values():
        for item in other.get("excluded_members", []):
            key = member_key(item["member_path"])
            if item["reason"] in DROPPED_FOR_GOOD_MEMBER_REASONS and key not in kept and is_raw_like(item["member_path"]):
                out.setdefault(key, (item["member_path"], item["reason"]))
    return out


def _dropped_product_ion_members(t: dict[str, Any], triages: dict[str, dict[str, Any]],
                                 resolver: "Resolver") -> list[tuple[str, str, list[str]]]:
    """(path, reason, words) for the members dropped for good whose names name product-ion spectra and do not put them
    in another arm than the unit's."""
    out = []
    for _, (path, why) in sorted(_members_dropped_for_good(triages, resolver).items()):
        words = product_ion_words(path)
        if words and not _not_its_arm(path, t):
            out.append((path, why, words))
    return out


def _loose_stem(name: str) -> str:
    """A stem without case, polarity words or separators: Inj001_S1_CSHposMSMS and Inj001_S1_CSHMSMS agree."""
    return re.sub(r"(?i)pos(?:itive)?|neg(?:ative)?|[^a-z0-9]", "", stem(basename(_norm_path(name)))).casefold()


def _dropped_raw_members(t: dict[str, Any], triages: dict[str, dict[str, Any]], resolver: "Resolver",
                         absent: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(path, reason) for the members dropped for good that may be the unit's own files: nothing in their names puts
    them in another arm, or their stems match a row the unit says the archive lacks, polarity infix and case aside."""
    wanted = {_loose_stem(item["raw_file"]) for item in absent if item.get("raw_file")}
    out = []
    for _, (path, why) in sorted(_members_dropped_for_good(triages, resolver).items()):
        if not _not_its_arm(path, t) or _loose_stem(path) in wanted:
            out.append((path, why))
    return out


def _check_unit(
    report: Report,
    unit: str,
    t: dict[str, Any],
    triages: dict[str, dict[str, Any]],
    catalog: dict[str, CatalogUnit] | None,
    listed: dict[str, ListedMember] | None,
    resolver: Resolver,
    answered: set[str] | None = None,
) -> None:
    answered = set(answered or ())
    v2 = t.get("schema") == SCHEMA_V2_ID
    status = t["status"]
    flags = _flag_codes(t)
    ut = t["unit_technical"]
    unit_ion, unit_chrom, unit_acq = ut["ion_mode"], ut["chromatography"], ut["acquisition_mode"]
    kept = _kept(unit, t, resolver)
    legacy = legacy_policy(t)

    # T-COVER: every Catalog row kept or excluded, once.
    count = t["catalog_row_count"]
    seen: dict[int, str] = {}
    for where, items in (("rows", t["rows"]), ("excluded_rows", t["excluded_rows"])):
        for item in items:
            i = item["row_index"]
            if i >= count:
                report.add("error", "T-COVER", unit, f"{where} names row {i}, but the handoff has {count} rows")
            elif i in seen:
                report.add("error", "T-COVER", unit, f"row {i} ({item['sample_id']}) is in {seen[i]} and {where}")
            else:
                seen[i] = where
    missing = [i for i in range(count) if i not in seen]
    if missing:
        shown = ", ".join(str(i) for i in missing[:10]) + (" ..." if len(missing) > 10 else "")
        report.add("error", "T-COVER", unit, f"{len(missing)} Catalog row(s) neither kept nor excluded: {shown}")

    if catalog is not None:
        cu = catalog.get(unit)
        if cu is None:
            report.add("error", "T-COVER", unit, f"the Catalog has no unit {unit}")
        elif cu.accession is not None and cu.accession != t["accession"]:
            report.add("error", "T-COVER", unit, f"the Catalog puts unit {unit} in {cu.accession}, the triage in {t['accession']}")
        elif cu.problem:
            report.add("error", "T-HANDOFF", unit, cu.problem)
        else:
            if cu.handoff_sha256 and cu.handoff_sha256 != t["catalog_handoff_sha256"]:
                report.add("error", "T-HANDOFF", unit, "catalog_handoff_sha256 does not match the Catalog handoff; triage again from the current handoff")
            if len(cu.rows) != count:
                report.add("error", "T-COVER", unit, f"catalog_row_count is {count}, the Catalog handoff has {len(cu.rows)} rows")
            for item in t["rows"] + t["excluded_rows"]:
                i = item["row_index"]
                if i < len(cu.rows):
                    row = cu.rows[i]
                    if row.get("sample_id") != item["sample_id"] or row.get("raw_file") != item["raw_file"]:
                        report.add("error", "T-COVER", unit, f"row {i} is {row.get('sample_id')}/{row.get('raw_file')} in the Catalog, not {item['sample_id']}/{item['raw_file']}")
            declared_chrom = normalise_chromatography(cu.declared.get("chromatography"))
            declared_ion = normalise_ion(cu.declared.get("ion_mode"))
            disputed = "declared_technical_disputed" in flags
            if declared_chrom not in ("unknown",) and unit_chrom != declared_chrom and not disputed:
                report.add("error", "T-DECL", unit, f"unit chromatography {unit_chrom} differs from the Catalog's {cu.declared.get('chromatography')!r} without a declared_technical_disputed flag")
            if declared_ion in ("positive", "negative") and unit_ion != declared_ion and not disputed:
                report.add("error", "T-DECL", unit, f"unit ion mode {unit_ion} differs from the Catalog's {cu.declared.get('ion_mode')!r} without a declared_technical_disputed flag")

    # T-EXCL: an exclusion that points at another unit must point at a real one that keeps the file, whether
    # the exclusion is of a Catalog row or of an archive member.
    for item in t["excluded_rows"] + t.get("excluded_members", []):
        target = item.get("belongs_to_unit")
        is_row = "row_index" in item
        what = f"row {item['row_index']} ({item['sample_id']}, {item['raw_file']})" if is_row else f"member {item['member_path']}"
        if item["reason"] == "belongs_to_other_unit":
            if not target:
                report.add("error", "T-EXCL", unit, f"{what} is excluded as another unit's without naming the unit")
            elif target == unit:
                report.add("error", "T-EXCL", unit, f"{what} is excluded as belonging to this same unit")
            elif target not in triages:
                report.add("hold", "T-EXCL", unit, f"{what} is said to belong to {target}, which has no triage file here")
            else:
                # Row indexes belong to each unit's own handoff, so the target is checked by file, not by index.
                path = item.get("member_path") or item["raw_file"]
                theirs = _kept(target, triages[target], resolver)
                same_row = (lambda k: k.is_row and k.item["sample_id"] == item["sample_id"] and k.item["raw_file"] == item["raw_file"]) if is_row else (lambda k: False)
                if not any(same_row(k) or same_file(k, path, resolver) for k in theirs):
                    report.add("hold", "T-EXCL", unit, f"{what} is said to belong to {target}, but {target} does not keep that file")

    # T-EXCL: a reason that contradicts the unit's own state drops a file with nobody asked.
    questions = {q["code"] for q in t["needs_person"]}
    for item in t["excluded_rows"] + t.get("excluded_members", []):
        reason = item["reason"]
        what = f"row {item['row_index']} ({item['sample_id']}, {item['raw_file']})" if "row_index" in item else f"member {item['member_path']}"
        if reason == "needs_person" and (status == "ready" or not t["needs_person"]):
            report.add("error", "T-EXCL", unit, f"{what} is excluded as needs_person, but the unit is {status} with {len(t['needs_person'])} question(s) for a person; ask the question or give the real reason")
        elif reason == "no_signal_abstained" and (status == "ready" or (status != "abstained" and "no_signal" not in questions)):
            report.add("error", "T-EXCL", unit, f"{what} is excluded as no_signal_abstained, but the unit is {status} with no no_signal question")
        elif reason == "polarity_switching" and "polarity_switching" not in flags:
            report.add("error", "T-EXCL", unit, f"{what} is excluded as polarity_switching without a polarity_switching flag")
        elif reason == "out_of_scope_ms1_only" and "ms1_only" not in flags:
            report.add("error", "T-EXCL", unit, f"{what} is excluded as out_of_scope_ms1_only without an ms1_only flag")
        elif reason == "ms1_only_no_msms":
            # P1 (2026-11-18): an MS1-only file carries no product-ion spectra and is left out, on evidence that settles
            # it (a declaration, or a name that contrasts with sibling MS/MS runs); a weaker reading keeps the file.
            names = [n for n in (item.get("member_path"), item.get("raw_file")) if n]
            said = sorted({word for n in names for word in product_ion_words(n)})
            # A row is the unit's own; an expanded payload lists the accession's members in every unit, so the flag
            # about a member may be another unit's.
            sources = [t] if "row_index" in item else [t, *(other for u, other in triages.items() if u != unit)]
            confirmed = [f for source in sources for f in source.get("flags", []) if f.get("code") == "ms1_only_excluded"
                         and f.get("certainty") == "confirmed" and _flag_names(f, item, member_only=source is not t)]
            if said:
                # Refused whatever else the payload says: a name that says MS/MS, DDA or DIA against an MS1-only reading
                # is a person's question, and an excluded unit asks nothing (T-STATUS refuses its blocking flags).
                report.add("error", "T-EXCL", unit, f"{what} is excluded as ms1_only_no_msms, but its name says "
                           f"{'+'.join(said)}, which names product-ion spectra; keep it, or, if the evidence still reads "
                           "MS1 only, flag name_token_disputed and send the unit to a person (needs_person)")
            elif not any(f.get("code") == "ms1_only_excluded" for source in sources for f in source.get("flags", [])):
                report.add("error", "T-EXCL", unit, f"{what} is excluded as ms1_only_no_msms without an ms1_only_excluded "
                           "flag (informational: it records the evidence and its certainty)")
            elif item.get("confidence") == "low" or not confirmed:
                report.add("error", "T-EXCL", unit, f"{what} is excluded as ms1_only_no_msms "
                           + ("at low confidence" if item.get("confidence") == "low" else
                              "without a confirmed ms1_only_excluded flag about it (a suspected reading is weak)")
                           + "; an MS1-only reading the evidence does not settle keeps the file, with an "
                           "ms1_only_suspected flag, for the header preflight to decide")
        elif reason == "shared_content_unassigned" and not (
                status == "excluded" and (t.get("exclusion") or {}).get("reason") == "shared_content_unassigned"):
            report.add("error", "T-EXCL", unit, f"{what} is excluded as shared_content_unassigned, but the unit is not "
                       "excluded as shared_content_unassigned (P5: both units whose arms hold byte-identical copies are "
                       "excluded until a post-download reassignment exists)")
        elif reason == "requires_conversion":
            runnable = _runnable_form(item, resolver)
            if runnable:
                report.add("error", "T-EXCL", unit, f"{what} is excluded as requires_conversion, but it is {runnable}, which MS-DIAL reads (requires_conversion is for mzXML and mzData)")

    # A file is kept or excluded by a unit, not both; and one acquisition is not kept twice in two formats.
    for e in t.get("excluded_members", []):
        both = [k for k in kept if same_file(k, e["member_path"], resolver)]
        if both:
            report.add("error", "T-EXCL", unit, f"{e['member_path']} is in excluded_members ({e['reason']}) and also kept ({both[0].path})")
    formats: dict[str, dict[str, str]] = defaultdict(dict)
    for k in kept:
        if k.resolved:
            formats[k.stem].setdefault(_extension(k.key), k.member or k.path)
    for paths in formats.values():
        if len(paths) > 1:
            shown_paths = sorted(paths.values())
            report.add("error", "T-UNIQ", unit, f"one acquisition is kept twice, in {len(paths)} formats: {', '.join(shown_paths)}; keep one and exclude the others as alternative_format_copy")

    # T-STATUS
    exclusion = t.get("exclusion")
    if status == "excluded":
        # The triage policy of 2026-11-18: a unit left out by a rule a person already accepted asks nothing, holds
        # nothing for a person and keeps nothing; the plan leaves it out with its reason.
        reason = (exclusion or {}).get("reason")
        if reason not in UNIT_EXCLUSION_REASONS:
            report.add("error", "T-STATUS", unit, f"an excluded unit names its exclusion reason ({', '.join(UNIT_EXCLUSION_REASONS)})")
        if kept:
            report.add("error", "T-STATUS", unit, f"an excluded unit keeps no file ({len(kept)} kept)")
        if t["needs_person"]:
            report.add("error", "T-STATUS", unit, f"an excluded unit asks nothing ({len(t['needs_person'])} question(s)); "
                       "a unit that asks is needs_person")
        if t.get("children"):
            report.add("error", "T-STATUS", unit, "an excluded unit has no children")
        dropped = [*t["excluded_rows"], *t.get("excluded_members", [])]
        held = [item for item in dropped if item["reason"] == "needs_person"]
        if held:
            report.add("error", "T-STATUS", unit, f"an excluded unit holds nothing for a person ({len(held)} needs_person exclusion(s))")
        # A unit in doubt is needs_person, not excluded: a blocking flag on an excluded unit would hold nobody.
        allowed = {"shared_content"} if reason == "shared_content_unassigned" else set()
        for code in sorted((flags & blocking_flags_for(t)) - allowed - answered):
            report.add("error", "T-STATUS", unit, f"an excluded unit carries the blocking flag {code}, which waits for a "
                       "person; a unit in doubt is needs_person with the question, not excluded")
        if reason == "no_product_ion_files":
            if not any(item["reason"] == "ms1_only_no_msms" for item in dropped):
                report.add("error", "T-STATUS", unit, "the unit is excluded as no_product_ion_files, but it leaves no file "
                           "out as ms1_only_no_msms")
            # The premise: no file of the unit with product-ion spectra is left. Its own rows go only as MS1-only, as
            # another unit's, as absent or as copies; and no member the accession drops for good may be its MS/MS run.
            for item in t["excluded_rows"]:
                if item["reason"] not in NO_PRODUCT_ION_ROW_REASONS:
                    report.add("error", "T-STATUS", unit, f"the unit is excluded as no_product_ion_files, but row "
                               f"{item['row_index']} ({item['raw_file']}) is left out as {item['reason']}; its own rows "
                               f"go only as {', '.join(sorted(NO_PRODUCT_ION_ROW_REASONS))}, or the unit asks a person")
            for path, why, words in _dropped_product_ion_members(t, triages, resolver):
                report.add("hold", "T-STATUS", unit, f"the unit is excluded as no_product_ion_files, but {path} (left out "
                           f"as {why}) is named {'+'.join(words)}, which names product-ion spectra, and nothing in its "
                           "name puts it in another arm; a person looks before the unit leaves the campaign")
        if reason == "no_files":
            own = [item for item in t["excluded_rows"] if item["reason"] not in ("not_in_archive", "belongs_to_other_unit")]
            if own:
                report.add("error", "T-STATUS", unit, f"the unit is excluded as no_files, but row {own[0]['row_index']} "
                           f"({own[0]['raw_file']}) is left out as {own[0]['reason']}, not as a file the archive lacks "
                           "or another unit's")
            # Its files may be in the archive under names the Catalog writes differently (a polarity infix, case).
            absent = [item for item in t["excluded_rows"] if item["reason"] == "not_in_archive"]
            for path, why in _dropped_raw_members(t, triages, resolver, absent):
                report.add("hold", "T-STATUS", unit, f"the unit is excluded as no_files, but the archive's {path} (left "
                           f"out as {why}) may be one of its files; a person looks before the unit leaves the campaign")
        if reason == "shared_content_unassigned":
            if "shared_content" not in flags:
                report.add("error", "T-STATUS", unit, "the unit is excluded as shared_content_unassigned without a "
                           "shared_content flag naming the byte-identical copies")
            if not any(item["reason"] == "shared_content_unassigned" for item in dropped):
                report.add("error", "T-STATUS", unit, "the unit is excluded as shared_content_unassigned, but it leaves "
                           "no file out as shared_content_unassigned")
    elif exclusion is not None:
        report.add("error", "T-STATUS", unit, f"status {status} with an exclusion; only an excluded unit names one")
    if status == "needs_person" and not t["needs_person"]:
        report.add("error", "T-STATUS", unit, "status needs_person with no question in needs_person")
    if status == "ready" and t["needs_person"]:
        report.add("error", "T-STATUS", unit, f"status ready while needs_person holds {len(t['needs_person'])} question(s)")
    if status == "abstained":
        if kept:
            report.add("error", "T-STATUS", unit, "an abstained unit keeps no rows or members")
        if not any(e["reason"] in ("no_signal_abstained", "needs_person") for e in t["excluded_rows"]) and count:
            report.add("error", "T-STATUS", unit, "an abstained unit must exclude its rows as no_signal_abstained")
    if status == "ready":
        if not kept:
            report.add("error", "T-STATUS", unit, "a ready unit keeps at least one file")
        if unit_ion in ("switching", "unknown"):
            report.add("error", "T-POL", unit, f"a ready unit cannot have ion mode {unit_ion}")
        if unit_chrom == "unknown":
            report.add("error", "T-CHROM", unit, "a ready unit cannot have unknown chromatography")
        for code in sorted(flags & blocking_flags_for(t)):
            if code not in answered:
                where = (f"a person_answer in {REVIEW_LOG} with question_code {code} for this payload or one it supersedes"
                         if v2 else f"person_answers code {code}")
                report.add("error", "T-STATUS", unit, f"flag {code} keeps the unit from being ready until a person answers it ({where})")
        if v2 and LOW_CONFIDENCE_CODE not in answered:
            for item in [*t["rows"], *t.get("added_members", [])]:
                if item.get("basis") == "inferred" and item.get("confidence") == "low":
                    label = item.get("member_path") or item.get("raw_file")
                    report.add("hold", "T-CONFIDENCE", unit, f"{label} is kept on an inferred reading at low confidence; a "
                               "ready unit does not rest on it until a person has looked (a person_answer in "
                               f"{REVIEW_LOG} with question_code {LOW_CONFIDENCE_CODE})")
        if unit_ion == "both" and not t.get("children"):
            report.add("error", "T-POL", unit, "a ready unit with both polarities needs children, one per polarity and acquisition")
    elif status != "excluded":
        questions = "; ".join(q["code"] for q in t["needs_person"])
        report.add("hold", "T-QUEUE", unit, f"status {status}" + (f": {questions}" if questions else ""))

    # Children partition the kept files, one polarity each.
    child_of: dict[str, dict[str, Any]] = {}
    child_paths: dict[str, str] = {}
    for child in t.get("children", []):
        if unit_ion in ("positive", "negative") and child["ion_mode"] != unit_ion:
            report.add("error", "T-POL", unit, f"child {child['child_key']} is {child['ion_mode']}, the unit is {unit_ion}; only a unit of both polarities has children of two")
        for p in child["member_paths"]:
            k = resolver.key(p)[0]
            if k in child_of:
                report.add("error", "T-CHILD", unit, f"{p} is in children {child_of[k]['child_key']} and {child['child_key']}")
            child_of[k] = child
            child_paths[k] = p

    def child_for(k: Kept) -> dict[str, Any] | None:
        if k.key in child_of:
            return child_of[k.key]
        hits = [child_of[c] for c, p in child_paths.items() if same_file(k, p, resolver)]
        return hits[0] if len(hits) == 1 else None

    if t.get("children"):
        for k in kept:
            if child_for(k) is None:
                report.add("error", "T-CHILD", unit, f"{k.path} is kept but in no child")
        for c, p in child_paths.items():
            if not any(k.key == c or same_file(k, p, resolver) for k in kept):
                report.add("error", "T-CHILD", unit, f"child member {p} is not kept by the unit")

    # Per file: polarity, chromatography, acquisition, format, evidence, tokens, headers.
    acqs: set[str] = set()
    acqs_by_run: dict[str, set[str]] = defaultdict(set)  # per child ("" for a unit without children)
    inclusion_list: list[str] = []
    for k in kept:
        item = k.item
        label = k.path
        ion = item["ion_mode"]
        child = child_for(k)
        # A row named only by raw_file is read through the member it resolves to: its folder and extension count.
        names = [label] + ([k.member] if k.member and k.member != label else [])
        shown = label if len(names) == 1 else f"{label} ({k.member})"
        if status != "out_of_scope" and any(x.casefold().endswith(CONVERSION_EXTENSIONS) for x in (k.path, k.key)):
            report.add("error", "T-FORMAT", unit, f"{shown} is mzXML or mzData, which MS-DIAL does not read; exclude it as requires_conversion (a reviewed conversion to mzML is a separate step)")
        expected_ion = child["ion_mode"] if child else unit_ion
        if ion == "switching" and "polarity_switching" not in flags:
            report.add("error", "T-POL", unit, f"{label} is polarity switching without a polarity_switching flag")
        if expected_ion in ("positive", "negative") and ion in ("positive", "negative") and ion != expected_ion:
            report.add("error", "T-POL", unit, f"{label} is {ion}, its {'child' if child else 'unit'} is {expected_ion}")
        # A polarity-split child runs its switching files at its own polarity (the decision of 2026-11-19).
        split_mode = (t.get("polarity_split") or {}).get("ion_mode")
        if expected_ion in ("positive", "negative") and ion in ("switching", "both") and not (
                ion == "switching" and split_mode == expected_ion and child is None):
            report.add("error", "T-POL", unit, f"{label} is {ion} inside a {expected_ion} {'child' if child else 'unit'}")
        if ion == "unknown":
            if status == "ready":
                report.add("error", "T-POL", unit, f"{label} has unknown polarity in a ready unit")
        if child and child["acquisition_mode"] not in ("mixed", "unknown") and item["acquisition_mode"] != child["acquisition_mode"]:
            report.add("error", "T-CHILD", unit, f"{label} is {item['acquisition_mode']}, its child {child['child_key']} is {child['acquisition_mode']}")
        chrom = item["chromatography"]
        if chrom not in ("unknown",) and unit_chrom not in ("unknown",) and chrom != unit_chrom:
            report.add("error", "T-CHROM", unit, f"{label} is {chrom}, the unit is {unit_chrom}")
        if chrom == "unknown" and status == "ready":
            report.add("error", "T-CHROM", unit, f"{label} has unknown chromatography in a ready unit")
        acqs.add(item["acquisition_mode"])
        acqs_by_run[child["child_key"] if child and not legacy else ""].add(item["acquisition_mode"])

        sources = {e["source"] for e in item["evidence"]}
        if item["basis"] == "declared" and not sources & DECLARED_SOURCES:
            report.add("error", "T-EVID", unit, f"{label} is marked declared but cites no repository declaration (catalog_row, catalog_unit, mwtab, repository_page)")
        if status == "ready" and all(e["source"] == "archive_listing" and e["field"].lower() in WEAK_FIELDS for e in item["evidence"]):
            report.add("error", "T-EVID", unit, f"{label} is kept on size or timestamp alone; abstain instead of guessing")

        if "name_token_disputed" not in flags:
            for name in names:
                tok_ion = token_polarity(name)
                if tok_ion and ion in ("positive", "negative") and tok_ion != ion:
                    report.add("error", "T-TOKEN", unit, f"{shown}: {name} says {tok_ion}, the triage says {ion} (flag name_token_disputed and ask a person if that is meant)")
                    break
            for name in names:
                tok_chrom = token_chromatography(name)
                if tok_chrom and chrom in ("HILIC", "RP") and tok_chrom != chrom:
                    report.add("error", "T-TOKEN", unit, f"{shown}: {name} says {tok_chrom}, the triage says {chrom} (flag name_token_disputed and ask a person if that is meant)")
                    break
            for name in names:
                problem = acquisition_token_problem(name, item["acquisition_mode"])
                if problem:
                    report.add("error", "T-TOKEN", unit, f"{shown}: {problem} (flag name_token_disputed and ask a person if that is meant)")
                    break
        if any(t in INCLUSION_LIST_TOKENS for name in names for t in token_acquisition(name)):
            inclusion_list.append(shown)

        for e in item["evidence"]:
            if e["source"] != "raw_header":
                continue
            field_name = e["field"].lower()
            value = str(e["value"])
            disagrees = False
            if field_name in ("polarity", "ion_mode") and normalise_ion(value) in ("positive", "negative", "switching") and normalise_ion(value) != ion:
                disagrees = True
            if field_name in ("acquisition_mode", "method") and value.upper() not in ("", "UNKNOWN"):
                header_acq = {"FULLSCAN": "MS1", "MS1": "MS1"}.get(value.upper(), value.upper())
                if header_acq.replace("-", "_") not in (item["acquisition_mode"].upper(),):
                    disagrees = True
            if disagrees and not ("folder_header_disagreement" in flags and status != "ready" or "folder_header_disagreement" in answered):
                report.add("error", "T-HEADER", unit, f"{label}: the raw header says {e['field']}={value}, the triage says otherwise; a folder/header disagreement goes to a person")

        if listed is not None and item.get("member_path") and member_key(item["member_path"]) not in listed:
            report.add("error", "T-MEMBER", unit, f"{item['member_path']} is not in the archive listing")
        if listed is not None and not k.resolved:
            report.add("error" if status == "ready" else "hold", "T-MEMBER", unit,
                       f"row {item['row_index']} ({label}) is kept with no member_path, and its name resolves to no single listed member; resolve it or exclude it")

    if "MS1" in acqs and "ms1_only" not in flags:
        report.add("error", "T-ACQ", unit, "MS1-only files are kept without an ms1_only flag")
    if "targeted_DDA" in acqs and "targeted_dda" not in flags:
        report.add("error", "T-ACQ", unit, "targeted DDA files are kept without a targeted_dda flag")
    if inclusion_list and "targeted_dda" not in flags:
        report.add("error", "T-ACQ", unit, f"{len(inclusion_list)} kept file(s) carry an inclusion-list DDA name token (tDDA, "
                   f"HERMES, targeted ddMS2; first: {inclusion_list[0]}) without a targeted_dda flag; inclusion-list DDA is "
                   "in scope, and the flag (informational, suspected is enough) is how a report names the MS2-selection bias")
    # Inclusion- or exclusion-list DDA is DDA (decision of 2026-11-18): targeted_DDA beside DDA is not a mix. Modes are
    # judged per run: children that each hold one acquisition mode split a unit's mixed acquisition (P2), not a mix.
    for run in sorted(acqs_by_run):
        modes = {"DDA" if a == "targeted_DDA" else a for a in acqs_by_run[run]} - {"unknown"}
        if len(modes) > 1 and "mixed_acquisition" not in flags:
            where = f"child {run}'s files" if run else "kept files"
            report.add("error", "T-ACQ", unit, f"{where} mix acquisition modes {sorted(modes)} without a mixed_acquisition "
                       "flag; split them into children, one per acquisition mode")
    if "unknown" in acqs and status == "ready" and unit_acq == "unknown":
        report.add("error", "T-ACQ", unit, "a ready unit with unknown acquisition")

    # shared_with: reciprocal, and the paths are kept by both.
    for share in t["shared_with"]:
        other = triages.get(share["unit_id"])
        if other is None:
            report.add("error", "T-SHARED", unit, f"shared_with names {share['unit_id']}, which has no triage file here")
            continue
        mine = {resolver.key(p)[0] for p in share["member_paths"]}
        theirs = set()
        for s in other["shared_with"]:
            if s["unit_id"] == unit:
                theirs |= {resolver.key(p)[0] for p in s["member_paths"]}
        if mine - theirs:
            report.add("error", "T-SHARED", unit, f"{len(mine - theirs)} path(s) shared with {share['unit_id']} are not declared back by it")

    for answer in t.get("person_answers", []):
        if not answer["by"].strip():
            report.add("error", "T-PERSON", unit, "a person answer must name the person")


def content_twins(listed: Iterable[ListedMember]) -> dict[str, set[str]]:
    """Every member key with a byte-identical twin (CRC32 and size, at least MIN_DUPLICATE_BYTES, raw-like; a vendor folder
    by the signature of its parts) and the keys of its twins. The one code path behind T-DUP, the digest's twin groups
    and the spec expander."""
    content: dict[tuple[str, int], list[ListedMember]] = defaultdict(list)
    folders: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for m in listed:
        mk = member_key(m.path)
        if VENDOR_FOLDER.match(_norm_path(m.path)):
            inner = _norm_path(m.path)[len(mk) :].casefold()
            folders[mk].append((inner, m.crc32, m.size))
        elif m.size >= MIN_DUPLICATE_BYTES and m.crc32 and is_raw_like(m.path):
            content[(m.crc32, m.size)].append(m)
    twins: dict[str, set[str]] = defaultdict(set)
    for group in content.values():
        keys = {member_key(m.path) for m in group}
        if len(keys) > 1:
            for k in keys:
                twins[k] |= keys - {k}
    by_signature: dict[tuple, list[str]] = defaultdict(list)
    for folder, parts in folders.items():
        by_signature[tuple(sorted(parts))].append(folder)
    for group in by_signature.values():
        if len(group) > 1:
            for k in group:
                twins[k] |= set(group) - {k}
    return dict(twins)


# Exclusion reasons that hold a file for a person rather than leave it out for good: a shared_content flag about copies a
# unit holds this way still waits for a person (decision of 2026-11-18, D2).
# Files a unit holds back without dropping them for good: for a person's answer (needs_person), or, in a unit excluded
# because its arm's copies are byte-identical to another arm's, until a post-download reassignment exists
# (shared_content_unassigned, P5). Both count as kept for the shared-content rule (D2).
HELD_REASONS = {"needs_person", "shared_content_unassigned"}
# Archive members that may hold raw data of their own, unopened: a row excluded as not_in_archive may be inside one.
NESTED_ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz")


def _item_key(item: dict[str, Any], resolver: Resolver) -> str | None:
    if item.get("member_path"):
        return member_key(item["member_path"])
    if item.get("raw_file"):
        return resolver.resolve(item["raw_file"])
    return None


def held_keys(triages: dict[str, dict[str, Any]], resolver: Resolver,
              reasons: Iterable[str] = HELD_REASONS) -> dict[str, set[str]]:
    """The files units hold back (excluded with a HELD_REASONS reason, or one of `reasons`), and by which units."""
    reasons = set(reasons)
    out: dict[str, set[str]] = defaultdict(set)
    for unit, t in triages.items():
        for item in [*t["excluded_rows"], *t.get("excluded_members", [])]:
            if item["reason"] in reasons:
                key = _item_key(item, resolver)
                if key:
                    out[key].add(unit)
    return out


def shared_content_subject(t: dict[str, Any], flag: dict[str, Any], resolver: Resolver) -> set[str]:
    """The files a shared_content flag is about: those it names (member_paths, row_indices); else those the unit's
    shared_content questions name; else the unit's own files: kept, held, and its rows excluded as duplicate_content.
    Never every file the payload lists: an expanded payload lists the accession's unreferenced members in every unit, so
    a member's exclusion says nothing about whose it is."""
    def named(entry: dict[str, Any]) -> set[str]:
        keys = {member_key(p) for p in entry.get("member_paths") or []}
        indices = set(entry.get("row_indices") or [])
        for item in [*t.get("rows", []), *t["excluded_rows"]]:
            if item["row_index"] in indices:
                key = _item_key(item, resolver)
                if key:
                    keys.add(key)
        return keys

    keys = named(flag)
    if not keys:
        for question in t.get("needs_person", []):
            if question.get("code") == "shared_content":
                keys |= named(question)
    if not keys:
        for item in [*t.get("rows", []), *t.get("added_members", [])]:
            key = _item_key(item, resolver)
            if key:
                keys.add(key)
        for item in [*t["excluded_rows"], *t.get("excluded_members", [])]:
            if item["reason"] in HELD_REASONS or ("row_index" in item and item["reason"] == "duplicate_content"):
                key = _item_key(item, resolver)
                if key:
                    keys.add(key)
    return keys


def shared_content_without_keepers(triages: dict[str, dict[str, Any]], twins: dict[str, set[str]],
                                   resolver: Resolver) -> list[tuple[str, bool]]:
    """(unit, whether its files have twins at all) for each shared_content flag none of whose byte-identical groups has a
    file that a unit keeps or holds for a person (D2): copies only among files excluded for good are
    duplicate_copies_excluded, informational. The expander (X-DUP-NOKEPT) and T-DUP share this.

    A copy a unit holds as shared_content_unassigned (P5) counts only for another unit's flag: a unit's own held copies
    say nothing about whether another arm holds the same bytes."""
    taken = ({k.key for unit, t in triages.items() for k in _kept(unit, t, resolver)}
             | set(held_keys(triages, resolver, HELD_REASONS - {"shared_content_unassigned"})))
    unassigned = held_keys(triages, resolver, {"shared_content_unassigned"})
    out = []
    for unit, t in triages.items():
        theirs = taken | {key for key, units in unassigned.items() if units - {unit}}
        for flag in t.get("flags", []):
            if flag.get("code") != "shared_content":
                continue
            concerned: set[str] = set()
            for key in shared_content_subject(t, flag, resolver) & set(twins):
                concerned |= {key} | twins[key]
            if not concerned & theirs:
                out.append((unit, bool(concerned)))
    return out


def _folder_key(key: str) -> str:
    return key.rsplit("/", 1)[0] if "/" in key else ""


def _check_cross_unit(report: Report, triages: dict[str, dict[str, Any]], listed: list[ListedMember], resolver: Resolver,
                      answered: dict[str, set[str]] | None = None) -> None:
    answered = answered or {}
    owners: dict[str, list[Kept]] = defaultdict(list)
    for unit, t in triages.items():
        for k in _kept(unit, t, resolver):
            owners[k.key].append(k)

    # One group per file. A row the index could not resolve ("raw:<stem>") may be any kept member of that
    # stem, so it joins the group of each of them; the members themselves stay distinct files (two units
    # keeping batch1/x.mzML and batch2/x.mzML do not share a file because a third names only "x").
    members_of_stem: dict[str, list[str]] = defaultdict(list)
    for key in owners:
        if not key.startswith("raw:"):
            members_of_stem[_key_stem(key)].append(key)
    files: dict[str, list[Kept]] = defaultdict(list)
    for key, holders in owners.items():
        targets = members_of_stem.get(_key_stem(key), []) if key.startswith("raw:") else [key]
        for target in targets or [key]:
            files[target] += holders

    def canonical(key: str) -> str:
        """The one kept member an unresolved name can mean, if there is exactly one."""
        targets = members_of_stem.get(_key_stem(key), []) if key.startswith("raw:") else []
        return targets[0] if len(targets) == 1 else key

    def declared_share(a: str, b: str, holders: list[Kept]) -> bool:
        def has(x: str, y: str) -> bool:
            mine = [h for h in holders if h.unit == x]
            return any(s["unit_id"] == y and any(same_file(h, p, resolver) for h in mine for p in s["member_paths"]) for s in triages[x]["shared_with"])

        return has(a, b) and has(b, a)

    def partners(a: str, b: str) -> bool:
        """Two children at their own opposite polarities naming each other as partners (the decision of 2026-11-20)."""
        one, other = triages[a].get("polarity_split"), triages[b].get("polarity_split")
        return (isinstance(one, Mapping) and isinstance(other, Mapping) and one.get("partner_unit_id") == b
                and other.get("partner_unit_id") == a and one.get("ion_mode") in POLARITY_SPLIT_MODES
                and other.get("ion_mode") == one.get("sibling_ion_mode") and one.get("ion_mode") == other.get("sibling_ion_mode"))

    # T-UNIQ: no file kept by two units unless both declare it, or both are partners (2026-11-20). One finding per pair.
    undeclared: dict[tuple[str, str], list[str]] = defaultdict(list)
    for holders in files.values():
        units = sorted({h.unit for h in holders})
        if len(holders) > len(units):
            report.add("error", "T-UNIQ", units[0], f"{holders[0].path} is kept twice by the same unit")
        for i, a in enumerate(units):
            for b in units[i + 1 :]:
                if not declared_share(a, b, holders) and not partners(a, b):
                    undeclared[(a, b)].append(holders[0].path)
    for (a, b), paths in sorted(undeclared.items()):
        report.add("error", "T-UNIQ", a, f"{len(paths)} file(s) kept by both {a} and {b} without shared_with on both (first: {sorted(paths)[0]})")

    # Identical kept file sets (the stage3b failure) are named once, on top of the per-file errors.
    sets: dict[frozenset[str], list[str]] = defaultdict(list)
    for unit, t in triages.items():
        keys = frozenset(canonical(k.key) for k in _kept(unit, t, resolver))
        if keys:
            sets[keys].append(unit)
    for keys, units in sets.items():
        if len(units) == 2 and partners(*units):
            continue
        if len(units) > 1:
            report.add("error", "T-UNIQ", units[0], f"units {', '.join(sorted(units))} keep the identical set of {len(keys)} files")

    if not listed:
        return

    # T-DUP: byte-identical content (CRC32 + size) anywhere in the listing.
    twins = content_twins(listed)

    # shared_content is for content a unit keeps or holds for a person (decision of 2026-11-18, D2): a flag none of
    # whose twin groups (of the files it is about) has such a file flags copies among files excluded for good, which
    # hold nothing (duplicate_copies_excluded, informational).
    for unit, has_twins in shared_content_without_keepers(triages, twins, resolver):
        report.add("error", "T-DUP", unit, "shared_content is flagged, but no unit keeps or holds for a person a file of "
                   "the byte-identical groups the flag is about" + ("" if has_twins else " (none of those files has a twin)")
                   + "; copies found only among files excluded for good are duplicate_copies_excluded (informational)")

    # One kept copy whose twins no unit keeps (K = 1) blocks only when a twin is another arm's: it lies in a folder where
    # another unit keeps or holds a file, or its name's polarity or chromatography token is not the kept copy's. A twin
    # in no other arm's place (a backup folder, a second export) is a copy excluded for good: duplicate_copies_excluded.
    held = held_keys(triages, resolver)
    placed: dict[str, set[str]] = defaultdict(set)
    for key, holders in owners.items():
        if not key.startswith("raw:"):
            placed[_folder_key(key)] |= {h.unit for h in holders}
    for key, units in held.items():
        placed[_folder_key(key)] |= units

    def other_arm(key: str, holder: Kept) -> str | None:
        for twin in sorted(twins[key]):
            if placed.get(_folder_key(twin), set()) - {holder.unit}:
                return twin
            path = resolver.original.get(twin, twin)
            polarity, chromatography = token_polarity(path), token_chromatography(path)
            if polarity and holder.item["ion_mode"] in ("positive", "negative") and polarity != holder.item["ion_mode"]:
                return twin
            if chromatography and holder.item["chromatography"] in ("HILIC", "RP") and chromatography != holder.item["chromatography"]:
                return twin
        return None

    # P5 leaves a unit out only for copies in another arm's place, the same test as above: a twin that another unit keeps
    # or holds in its folder, or a twin whose polarity or chromatography token is not the unit's. A twin inside the unit's
    # own files, or only in a backup folder or a second export, is a copy (D2): duplicate_content and
    # duplicate_copies_excluded, and the unit stays.
    for unit, t in triages.items():
        for item in [*t["excluded_rows"], *t.get("excluded_members", [])]:
            if item["reason"] != "shared_content_unassigned":
                continue
            key = _item_key(item, resolver)
            if key is None or key not in twins:
                continue  # reported below: no byte-identical twin at all
            arm = None
            for twin in sorted(twins[key]):
                if placed.get(_folder_key(twin), set()) - {unit} or _not_its_arm(resolver.original.get(twin, twin), t):
                    arm = twin
                    break
            if arm is None:
                name = item.get("member_path") or item.get("raw_file")
                what = f"row {item['row_index']} ({name})" if "row_index" in item else name
                report.add("error", "T-EXCL", unit, f"{what} is excluded as shared_content_unassigned, but none of its "
                           "byte-identical twins is in another arm's place (kept or held by another unit, in its folder, or "
                           "under another arm's name tokens); copies inside one arm or in a backup folder are D2 copies: "
                           "exclude the copy as duplicate_content, flag duplicate_copies_excluded, and keep the unit")

    owner_of = {key: sorted({h.unit for h in holders}) for key, holders in owners.items()}
    reported: set[tuple[str, frozenset[str], str]] = set()
    for key, holders in owners.items():
        if key not in twins:
            continue
        for unit in sorted({h.unit for h in holders}):
            t = triages[unit]
            holder = next(h for h in holders if h.unit == unit)
            twin_units = sorted({u for tw in twins[key] for u in owner_of.get(tw, [])} - {unit})
            twin_inside = any(unit in owner_of.get(tw, []) for tw in twins[key])
            arm = other_arm(key, holder) if not twin_units and not twin_inside else None
            kind = "copy" if not twin_units and not twin_inside and arm is None else "shared"
            tag = (unit, frozenset(twin_units), kind)
            settled = "shared_content" in _flag_codes(t) and (t["status"] != "ready" or "shared_content" in answered.get(unit, set()))
            if twin_inside and not settled:
                report.add("error", "T-DUP", unit, f"{holders[0].path} has a byte-identical twin kept by the same unit")
            if tag in reported:
                continue
            reported.add(tag)
            if kind == "copy" and not settled:
                if "duplicate_copies_excluded" not in _flag_codes(t):
                    report.add("error", "T-DUP", unit, f"{holders[0].path} is byte-identical (CRC32 and size) to a copy no unit "
                               "keeps, in no other arm's folder and under no other arm's name tokens; exclude the copy as "
                               "duplicate_content and flag duplicate_copies_excluded (informational), or flag shared_content "
                               "if the copy may be another arm's")
                continue
            where = (f"kept by {', '.join(twin_units)}" if twin_units else
                     f"elsewhere in the archive, in another arm's place ({resolver.original.get(arm, arm)})" if arm else
                     "elsewhere in the archive")
            if settled:
                report.add("hold", "T-DUP", unit, f"byte-identical content shared with files {where} (first: {holders[0].path}); flagged, waiting for a person")
            else:
                report.add("error", "T-DUP", unit, f"{holders[0].path} is byte-identical (CRC32 and size) to a file {where}; shared content is never settled by name: flag shared_content and send the unit to a person")

    # T-EXCL against the listing: an exclusion reason the listing contradicts drops a file silently. A file said not to be
    # in the archive may be inside an archive the listing holds unopened: a person looks first.
    nested = sorted({_norm_path(m.path) for m in listed if _norm_path(m.path).casefold().endswith(NESTED_ARCHIVE_EXTENSIONS)
                     and is_raw_like(m.path) is False and not basename(m.path).startswith("._")})
    for unit, t in triages.items():
        unopened = 0
        for item in t["excluded_rows"] + t.get("excluded_members", []):
            name = item.get("member_path") or item.get("raw_file") or ""
            what = f"row {item['row_index']} ({name})" if "row_index" in item else name
            if item["reason"] == "not_in_archive":
                there = {member_key(item["member_path"])} & resolver.members if item.get("member_path") else resolver.hits(name)
                if there:
                    report.add("error", "T-EXCL", unit, f"{what} is excluded as not_in_archive, but the listing has {sorted(there)[0]}")
                elif nested:
                    unopened += 1
            elif item["reason"] in ("duplicate_content", "shared_content_unassigned"):
                key = member_key(item["member_path"]) if item.get("member_path") else resolver.resolve(name)
                if key is None or key not in twins:
                    report.add("error", "T-EXCL", unit, f"{what} is excluded as {item['reason']}, but the listing shows no byte-identical twin (CRC32 and size) for it")
        if unopened:
            report.add("hold", "T-EXCL", unit, f"{unopened} row(s) are excluded as not_in_archive, but the listing holds "
                       f"{len(nested)} unopened archive(s) ({', '.join(nested[:3])}); their files may be inside one, so a "
                       "person looks before they are dropped")

    # Raw-looking members that no unit kept or excluded. A row without a member path accounts for the
    # member of the same name, or of the same stem when the row's raw_file carries no extension.
    accounted: set[str] = set(owners)
    for t in triages.values():
        accounted |= {member_key(e["member_path"]) for e in t.get("excluded_members", [])}
        for item in t["rows"] + t["excluded_rows"]:
            if item.get("member_path"):
                accounted.add(member_key(item["member_path"]))
            else:
                accounted.add(_name_key(item["raw_file"]))
    loose: list[str] = []
    for m in listed:
        if not is_raw_like(m.path):
            continue
        if member_key(m.path) in accounted or "name:" + basename(m.path).casefold() in accounted or "stem:" + stem(m.path).casefold() in accounted:
            continue
        loose.append(m.path)
    loose = sorted(set(member_key(p) for p in loose))
    if loose:
        shown = ", ".join(loose[:5]) + (" ..." if len(loose) > 5 else "")
        report.add("hold", "T-MEMBER", None, f"{len(loose)} raw member(s) in the archive that no unit kept or excluded: {shown}")


# ----------------------------------------------------------------------------------------- scoring


def score(triage_dir: Path, ground_truth: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per unit: correct, wrong_included, wrongly_left_out, held_for_person, against ground_truth.json (eval fixtures).

    held_for_person counts the ground truth's members the unit excludes as needs_person: held for the person its
    question asks (shared byte-identical copies, skill 1.2.0), neither kept nor dropped, so not wrongly left out.

    A ground-truth unit is named by its public analysis id. The eval fixtures' payloads are named the same way; a
    campaign's are named by the Catalog unit id and carry the analysis id as source_subrecord_id, so the current
    payload whose source_subrecord_id is the analysis id is the one scored when no <analysis id>.json exists."""
    by_subrecord: dict[str, Path] = {}
    for current in current_triage_files(triage_dir):
        try:
            subrecord = json.loads(current.read_text(encoding="utf-8")).get("source_subrecord_id")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(subrecord, str) and subrecord:
            by_subrecord.setdefault(subrecord, current)
    unchecked = {member_key(m["path"]) for m in ground_truth.get("unchecked_members", [])}
    unchecked_names = {basename(m["path"]).casefold() for m in ground_truth.get("unchecked_members", [])}
    overrides = {k: _norm_path(v) for k, v in ground_truth.get("row_member_overrides", {}).items()}
    out: dict[str, dict[str, int]] = {}
    for unit in ground_truth["units"]:
        uid = unit["unit_id"]
        truth = {member_key(m["path"]) for m in unit["members"]}
        truth_by_name: dict[str, str] = {}
        for m in unit["members"]:
            truth_by_name.setdefault(basename(m["path"]).casefold(), member_key(m["path"]))
            truth_by_name.setdefault(stem(m["path"]).casefold(), member_key(m["path"]))
        path = triage_dir / f"{uid}.json"
        if not path.exists() and uid in by_subrecord:
            path = by_subrecord[uid]
        kept: set[str] = set()
        other: set[str] = set()
        held: set[str] = set()
        if path.exists():
            t = json.loads(path.read_text(encoding="utf-8"))
            for item in [*t.get("excluded_rows", []), *t.get("excluded_members", [])]:
                if item.get("reason") in HELD_REASONS and item.get("member_path"):
                    held.add(member_key(item["member_path"]))
            for row in t.get("rows", []):
                if row.get("member_path"):
                    kept.add(member_key(row["member_path"]))
                    continue
                raw = row["raw_file"]
                if raw in overrides:
                    kept.add(member_key(overrides[raw]))
                    continue
                name = basename(raw).casefold()
                hit = truth_by_name.get(name) or truth_by_name.get(stem(raw).casefold())
                if hit:
                    kept.add(hit)
                elif name not in unchecked_names:
                    other.add("raw:" + name)
            for member in t.get("added_members", []):
                kept.add(member_key(member["member_path"]))
        kept -= unchecked
        held = (held - kept) & truth
        out[uid] = {
            "correct": len(kept & truth),
            "wrong_included": len(kept - truth) + len(other),
            "wrongly_left_out": len(truth - kept - held),
            "held_for_person": len(held),
        }
    return out


# ---------------------------------------------------------------------------------------------- CLI


def _render(report: Report) -> str:
    lines = [f"triage {report.accession or '?'}: {report.verdict.upper()}"]
    for unit in report.units:
        lines.append(f"  {unit}: status {report.unit_status.get(unit, '?')}, {report.unit_verdict(unit)}")
    shown: dict[tuple[str, str, str | None], int] = defaultdict(int)
    for f in report.findings:
        group = (f.level, f.check, f.unit)
        shown[group] += 1
        if shown[group] <= 10:
            lines.append(f"  [{f.level}] {f.check} {f.unit or '-'}: {f.message}")
    for (level, check, unit), n in shown.items():
        if n > 10:
            lines.append(f"  [{level}] {check} {unit or '-'}: ... {n - 10} more (all of them with --json)")
    if report.score is not None:
        lines.append("  score against ground truth:")
        for unit, s in report.score.items():
            lines.append(f"    {unit}: correct {s['correct']}, wrong_included {s['wrong_included']}, wrongly_left_out "
                         f"{s['wrongly_left_out']}, held_for_person {s['held_for_person']}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ --emit-selection (section 2.4)


def _curated():
    """scripts/campaign/curated.py, the runner's reader and the review log's only writer."""
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from campaign import curated  # noqa: PLC0415

    return curated


def emit_selection(report: Report, triage_dir: Path, *, at: str | None = None, commit: str | None = None,
                   append: bool = True) -> dict[str, Any]:
    """Each judged decision's selection and its validator_verdict entry in the review log.

    The selection is msdial-selection-canonical.v1 over the archive listing the unit's decision record binds
    (curated.record_inputs, as the plan and Interactive read it; none without a record), per unit and per child. The
    verdict entry (tool, code_commit, verdict, findings_sha256, findings) is appended once per decision through
    curated.append_review_log, the unit's findings with the accession's own; an entry that would repeat the decision's
    last verdict entry (same code_commit, verdict and findings) is not appended again. A review log whose chain is
    broken is not appended to."""
    curated = _curated()
    triage_dir = Path(triage_dir)
    at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    log, problems = read_review_log(triage_dir)
    last: dict[str, dict[str, Any]] = {}
    for entry in log:
        if entry.get("kind") == "validator_verdict":
            last[str(entry.get("decision_id"))] = entry
    selection: dict[str, Any] = {}
    appended, repeated = [], []
    for unit in report.units:
        info = report.payloads.get(unit)
        if not info:
            continue
        decision_id = info["decision_id"]
        items = [{"check": f.check, "level": f.level, "message": f.message} for f in report.findings if f.unit in (unit, None)]
        levels = {item["level"] for item in items}
        verdict = "error" if "error" in levels else "hold" if "hold" in levels else "pass"
        findings_sha256 = sha256_bytes(canonical_json(items))
        block: dict[str, Any] = {"decision_id": decision_id, "verdict": verdict,
                                 "selection_canonical_form": curated.SELECTION_CANONICAL_FORM}
        path = triage_dir / info["payload"]
        if unit in report.unit_status:
            triage = json.loads(path.read_bytes().decode("utf-8"))
            # What a report notes and nothing holds (targeted_dda: the MS2-selection bias of inclusion-list DDA).
            block["report_flags"] = [{"code": f["code"], "certainty": f["certainty"], "detail": f["detail"]}
                                     for f in triage.get("flags", []) if f.get("code") in INFORMATIONAL_FLAGS]
            record = curated.record_path(path)
            bound = curated.record_inputs(record, decision_id) if record.is_file() else None
            if bound is not None and bound["problem"]:
                block.update(selection_sha256=None, problem=bound["problem"])
            else:
                listing = bound["listing"] if bound is not None else {}
                block.update(
                    record=record.name if bound is not None else None,
                    selection_listings=(bound or {}).get("archive_listings") or [],
                    selection_count=len(curated.selection_canonical(triage, listing)),
                    selection_sha256=curated.selection_sha256(triage, listing),
                    children={str(child["child_key"]): curated.selection_sha256(triage, listing, str(child["child_key"]))
                              for child in triage.get("children") or []},
                )
        else:
            block.update(selection_sha256=None, problem="the payload is not a valid triage")
        selection[unit] = block
        previous = last.get(decision_id)
        if previous is not None and (previous.get("code_commit"), previous.get("verdict"), previous.get("findings_sha256")) \
                == (commit, verdict, findings_sha256):
            repeated.append(unit)
            continue
        if append and not problems:
            curated.append_review_log(triage_dir, "validator_verdict", decision_id, at, tool=TOOL_NAME, code_commit=commit,
                                      verdict=verdict, findings_sha256=findings_sha256, findings=items)
            appended.append(unit)
    review_log: dict[str, Any] = {"path": REVIEW_LOG, "appended": appended, "already_recorded": repeated,
                                  "head_sha256": curated.review_log_head(triage_dir)}
    if problems:
        review_log["refused"] = f"{REVIEW_LOG}'s chain does not hold ({problems[0]}); nothing was appended"
    elif not append:
        review_log["refused"] = "a release bundle is never written to; nothing was appended"
    report.extra["selection"] = selection
    report.extra["review_log"] = review_log
    return report.extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("triage_dir", type=Path)
    parser.add_argument("--catalog", type=Path, help="handoffs directory or catalog.json")
    parser.add_argument("--archive-listing", type=Path, action="append", default=[], help="archive member TSV (repeatable)")
    parser.add_argument("--ground-truth", type=Path, help="ground_truth.json to score against")
    parser.add_argument("--code-root", type=Path, help="the released code (skill, prompt templates) at the records' commit, "
                        "read in place of git")
    parser.add_argument("--release-bundle", type=Path, help="the release bundle the triage directory sits in (found on "
                        "its own when <triage dir>/../../release-index.json exists)")
    parser.add_argument("--emit-selection", action="store_true", help="add each unit's selection to the report and append "
                        "a validator_verdict per decision to the accession's review-log.jsonl")
    parser.add_argument("--payload-only", action="store_true", help="check the payloads alone: no decision record, "
                        "transcript, context or revision check (the in-session spec check)")
    parser.add_argument("--polarity-split", type=Path, action="append", default=[],
                        help="a polarity-split child payload (the decision of 2026-11-19), checked in place of its "
                        "parent's payload in this directory (repeatable, one per unit)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if not args.triage_dir.is_dir():
        print(f"not a directory: {args.triage_dir}", file=sys.stderr)
        return 2
    bundle = None
    if args.release_bundle is not None:
        if not (args.release_bundle / RELEASE_INDEX).is_file():
            print(f"not a release bundle (no {RELEASE_INDEX}): {args.release_bundle}", file=sys.stderr)
            return 2
        bundle = ReleaseBundle(args.release_bundle)
    else:
        bundle = ReleaseBundle.find(args.triage_dir)
    catalog = read_catalog(args.catalog, bundle) if args.catalog else None
    listed: list[ListedMember] = []
    for path in args.archive_listing:
        listed += read_listing(path)
    report = validate(args.triage_dir, catalog, listed, code_root=args.code_root, bundle=bundle,
                      listing_files=args.archive_listing, payload_only=args.payload_only,
                      polarity_split=args.polarity_split)
    if args.ground_truth:
        report.score = score(args.triage_dir, json.loads(args.ground_truth.read_text(encoding="utf-8")))
    if args.emit_selection:
        emit_selection(report, args.triage_dir, commit=code_commit(), append=bundle is None)
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False) if args.json else _render(report))
    if args.emit_selection and bundle is not None:
        print("--emit-selection does not write into a release bundle; the review log was left as it is", file=sys.stderr)
        return 2
    return {"pass": 0, "hold": 1, "error": 2}[report.verdict]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
