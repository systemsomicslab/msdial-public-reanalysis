"""The contract states the rules in force, and each name and rule it states must hold in this tree.

CLAUDE.md, README.md, AGENTS.md and the project skills tell a session which script to run, which record to read and
which template to return. A name that resolves to nothing sends that session looking for a file that is not there.
Locations outside this repository (the Interactive checkout, the analysis roots) are not checked: they are not this
tree's to hold.

The documents also name gate checks, and the campaign's gate rule decides by check which before-production FAILs stop
a unit's run (the user's decisions of 2026-10-01 and 2026-10-02, PAIR-1's place of 2026-10-08). Every named check must
be one the gate runs, or one a pending pull request brings (PENDING_CHECKS, which fails once the gate runs it); the
rule must place every before-production check the gate runs; and the two lists must hold exactly what the trial
manifest's latest decision gives them and the gate's RUN_POLICY states.

Since 2026-10-27 CLAUDE.md is a statement of the rules in force, and the history of each decision, with the user's
words, is in the trial manifest. So these tests hold the contract to each rule the user decided, to the runner's own
held states, pauses and boundaries, to the pull requests it marks as pending, and to what it no longer states; and
they hold the trial manifest to the decisions it records, in order. No document names a private library location.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import inspect
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = (
    "CLAUDE.md",
    "README.md",
    "AGENTS.md",
    ".claude/skills/msdial-repository-batch/SKILL.md",
    ".claude/skills/msdial-lipidomics-workflow/SKILL.md",
)
_CONTRACT = "CLAUDE.md"
_BATCH_SKILL = ".claude/skills/msdial-repository-batch/SKILL.md"
_TRIAL_MANIFEST = "trials/2026-09-20-ten-unit-trial-manifest.json"
# The top-level folders of this repository. A code span whose first segment is one of them names a file here; any
# other span (a workspace path, a manifest field, an argument) is not a claim about this tree.
_REPOSITORY_FOLDERS = frozenset({".claude", "feedback", "prompts", "scripts", "tests", "trials"})
# The documents also name this repository by its absolute location on the author's machine, with either separator.
_ABSOLUTE_ROOT = re.compile(r"D:[\\/]13_MSDIAL_Public_Reanalysis[\\/]code[\\/]([^\s`)'\"]+)", re.IGNORECASE)
_FENCE = re.compile(r"^```.*?^```[^\n]*$", re.MULTILINE | re.DOTALL)
# A code span may break across a line, as Markdown lets it, but never across a blank line.
_SPAN = re.compile(r"`((?:[^`\n]|\n(?![ \t]*\n))+)`")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
# A file:line reference (scripts/x.py:2760, or :10-20) names the file.
_LINE_REFERENCE = re.compile(r":\d+(?:-\d+)?$")

PENDING: dict[str, str] = {}
# Named on purpose and kept out of the repository by .gitignore: audit outputs under feedback/ are local by default.
LOCAL_ONLY = frozenset({"feedback/codex-pre-audit-2026-09-02.md"})

# A gate check id: letters, a hyphen and one digit (SUM-1, CONV-1). Tokens of that shape that are not checks are
# listed here.
_CHECK_ID = re.compile(r"\b[A-Z]{2,5}-[1-9]\b")
_NOT_CHECKS = frozenset({"UTF-8"})
# Checks the documents name that a pull request not yet merged brings, with the list the contract gives them and the
# pull request. The test fails once the gate runs the check, so that the entry is removed and the check placed in its
# bullet rather than left pending.
PENDING_CHECKS: dict[str, tuple[str, str]] = {
    "LIB-2": ("blocks_run", "gate truthful-libraries (feat/truthful-libraries-shared-downloads-root)"),
}
_GATE_RULE_SECTION = "## Gate verdicts in a campaign"
_SCOPE_SECTION = "## Supported production scope"
_EVIDENCE_SECTION = "## Evidence and decisions"
_ANNOTATION_SECTION = "## Annotation and private resources"
_BOUNDARIES_SECTION = "## Confirmation boundaries"
_RETENTION_SECTION = "## Raw-data retention"
_STORAGE_SECTION = "## Local storage"
_MERGES_SECTION = "## Merges"
_PENDING_SECTION = "## Pending pull requests"
_GATE_LISTS = ("blocks_run", "record_only")
# The lists as the user gave them on 2026-10-02, with PAIR-1 placed on 2026-10-06 and decided by the user on 2026-10-08
# (answer 1). A later decision changes this test as well as the trial manifest.
_DECIDED_NOW = {
    "blocks_run": {"ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1", "ID-1", "PRE-2", "CONV-1"},
    "record_only": {"CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1", "SPL-1", "PRE-1", "PAIR-1"},
}
# The paragraph of the gate rule that names the before-production checks the user placed in neither list.
_NEITHER_LIST = "**In neither list.**"
# What marked a passage as the contract's reading of a case the user had not settled. No passage carries it.
_AWAITING = "awaiting the user's decision"
_DISCARD_FALLBACK_FIELD = "confirmed_discard_fallback"
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*]|\d+\.)[ \t]")

# Each case the user settled, as the contract states it: the section, the lead of the one passage that states it, and
# what that passage must say.
_DECIDED_CASES = {
    "not evaluable": (_GATE_RULE_SECTION, "**Not evaluable.**", (
        "`blocks_run` check left `not_evaluable` where it is required stops the run exactly as its FAIL does",
        "`strict_failures`", "never stops the run",
    )),
    "no report": (_GATE_RULE_SECTION, "**No report.**", (
        "holds the unit", "`gate_held`", "its raw data kept", "counted neither as a retry nor as a failure",
        "every other unit goes on", "runs the gate again", "`run --until-idle` does not return while a unit is held",
        "`scripts/campaign-runner.py recheck-held`", "one must be started", "a missing report is only recorded",
    )),
    "a stop is per unit": (_GATE_RULE_SECTION, "**A stop is per unit.**", (
        "never the runner",
        "A pin change, a short disk, a repository outage and an Interactive backend that does not answer",
        "pause the whole campaign", "the four pauses the status export names", "each lifts by itself",
        "Nothing one unit does pauses the campaign", "`contract_held`", "a reply that does not parse",
        "never taken for a lost job",
    )),
    "held by Interactive": (_GATE_RULE_SECTION, "**Held by Interactive.**", (
        "`disposition_held`", "its raw data kept", "counted neither as a retry nor as a failure",
        "not rechecked by itself", "needs no OK", "Only an operator's `skip` releases the hold",
        "only on the user's explicit word for that unit", "boundary 5 does not cover it",
    )),
    "acquisition, header first": (_SCOPE_SECTION, "- **Acquisition, header first (rule B2).**", (
        "the header decides over the repository's declaration", "keyword inference", "excluded with its reason",
        "not folded into a DDA run", "ACQ-1 FAILs", "`acquisition_declared_only`",
    )),
    "AIF": (_SCOPE_SECTION, "- **AIF** goes by", (
        "it runs as SWATH", "MsdialWorkbench #825", "`aif_multi_ce_awaiting_console`",
        "`aif_energy_sets_differ_between_inputs`", "its path relative to the unit's raw data root",
        "holds the whole unit", "`aif_collision_energy_unrecorded`",
    )),
    "mzXML": (_SCOPE_SECTION, "- **mzXML and mzData**", (
        "`requires_conversion`", "Interactive's validated converter", "`raw\\converted`",
        "exactly one polarity", "`technical_settings.ion_mode`", "recorded as an inference", "nothing is imputed",
        "CONV-1 FAILs", "mix the opposite polarity", "mzData has no converter",
    )),
    "ion mobility from unit-level evidence": (_SCOPE_SECTION, "- **Ion mobility from unit-level evidence.**", (
        "only on its own evidence", "is not such evidence", "`ion_mobility_evidence` state is `enabled`",
        "MTBKS219 and MTBKS220", "whose Catalog record the plan cannot read", "pass to the raw-header check",
    )),
    # 2026-10-15.
    "folders inside one per-sample archive": (_SCOPE_SECTION, "- **Folders inside one per-sample archive are copies.**", (
        "one is used, by the one encoding rule, and the others are recorded", "even where their bytes differ",
        "still refused",
    )),
    "declared inputs partly openable": (_SCOPE_SECTION, "- **A unit whose declared inputs are only partly", (
        "is excluded whole", "`declared_inputs_unsupported_format`", "not retried",
    )),
    # 2026-10-24, D4.
    "declared Both polarity": (_SCOPE_SECTION, "- **Declared \"Both\" polarity.**", (
        "outside the campaign's pools for now", "later stage", "after polarity splitting has been validated",
    )),
}
# The rules decided from 2026-09-20 to 2026-10-27 that the contract states, each as phrases its section must hold.
_RULES = {
    "one campaign-level approval": (_BOUNDARIES_SECTION, (
        "except where an approved campaign's approval stands in for one",
        "Interactive work outside a campaign is confirmed per run",
        "Inside a campaign there are no per-unit confirmations",
        "`scripts/campaign-runner.py approve`", "boundaries 1, 3 and 4, for the split",
        "for boundary 5 when the manifest's retention is `delete_after_validated_output`",
        "It never covers boundary 2 or 6", "Only `approve` creates a campaign's ledger",
    )),
    "the go signal": (_BOUNDARIES_SECTION, (
        "本番開始前には私がGoサインを出すので、聞いてください。", "is never started on a real campaign without it",
    )),
    "pins": (_BOUNDARIES_SECTION, (
        "A change to any pin pauses the campaign",
        "built from the latest MsdialWorkbench master",
        "local RawDataHandler pin on a local-only branch that is never pushed", "The Console is pinned by sha256",
        "the campaign runs it from a frozen worktree at that commit",
        "Any commit to this repository during a campaign is a pin change and pauses it",
        "this contract merges before a production plan is made",
        "no crawl starts unless the user asks",
    )),
    "raw-data retention": (_RETENTION_SECTION, (
        "The retention policy is never implied",
        "**Full-repository reanalysis** (`delete_after_validated_output`)", "its mzTab-M validates",
        "whatever the gate's verdict", "once a failed unit has been retried twice", "skipped or excluded",
        "**Trials and pilots** keep their raw data (`keep`)", "**Outside a campaign** the default is `keep`",
        "keeps the loaded-library copy once in `_library_copies`",
        "claims each shared object for every sibling unit",
    )),
    "local storage": (_STORAGE_SECTION, (
        "The workspace root is chosen per campaign and passed explicitly",
        "F:\\MSDIAL_Public_Reanalysis\\analysis      MB-POST production (dedicated HDD)",
        "D:\\13_MSDIAL_Public_Reanalysis\\analysis   archive of the pilots and preflights",
        "Any other root needs the user's explicit confirmation",
        "never go to C:, a user profile, a temporary folder or a code checkout",
        "`_library_copies`, the library-copy store: one MS-DIAL loaded-library copy (`.dbs`) per library set",
    )),
    "annotation tiers": (_ANNOTATION_SECTION, (
        "four tiers, kept apart as evidence", "LBM2 rule-based lipid annotation",
        "strict search of the acquired MSP pair", "broad search of the same acquired MSP pair",
        "the in-silico MSP GLACIER", "non-public NIST20-trained build of coleygroup/ms-pred",
        "searched without RT and offers no suggestions", "MS1 tolerance 0.01 Da", "MS2 tolerance 0.05 Da",
        "0.5/0.5/0.5", "at least 3 matched peaks", "never receive the lipid RT check's \"Confirmed\" tag",
        "An accepted match of a lower tier outranks a higher tier's precursor-only suggestion",
        "by their Evidence source (`InSilico`)", "GLACIER is never redistributed",
    )),
    "the LBM2 and its cutoffs": (_ANNOTATION_SECTION, (
        "The campaign LBM2 is the one the campaign profile names",
        "`Msp20261013094720_NCDK-TUAT-LC25_no-unknown-db.lbm2`",
        "`Msp20250303164224_NCDK-TUAT-LC25_converted_dev.lbm2`", "Its cutoffs are 0.1/0.1/0.1",
    )),
    "target omics": (_ANNOTATION_SECTION, (
        "Every campaign unit is an MS-DIAL Lipidomics project",
        "every MSP annotator row pins its own target omics, Metabolomics",
    )),
    "the lipid RT check": (_ANNOTATION_SECTION, (
        "never left to the Console's defaults", "the run records its setting and its review file",
    )),
    "records state what ran": (_ANNOTATION_SECTION, (
        "**Records state what ran, not what was pinned.**",
        "Gate LIB-2, a `blocks_run` check, stops a run whose libraries",
    )),
    "merges": (_MERGES_SECTION, (
        "The user merges pull requests", "Claude opens branches and pull requests and pushes to them",
        "never by force", "merges only once the user has approved its wording",
    )),
    "RT correction": (_EVIDENCE_SECTION, (
        "**Automatic RT correction** is on in a campaign", "12 anchors", "`plan --policy`", "`policy_overrides`",
        "A manifest that carries no automatic RT correction statement is refused",
        "the unit's later attempts run uncorrected", "The zero-threshold diagnostic never corrects",
    )),
    "the one encoding rule": (_EVIDENCE_SECTION, (
        "exactly one is used", "then mzML, then mzXML", "the first by relative path",
        "the next in order is taken", "whatever encoding the row names", "`manifest.encoding_choices`",
        "Gate INP-1 recomputes the rule", "That is an implementation gap, not policy",
    )),
    "peak height": (_EVIDENCE_SECTION, (
        "at the lower end", "100 for QTOF-type data and 1,000 for Fourier-transform data",
        "fall back to 10 (Fourier-transform: 100), never finer", "`TimeBasedLinearWeightedMovingAverage`",
    )),
}
# The pull requests the contract marks as pending, by the label its marks use, and what the Pending pull requests
# section must name for each.
_PENDING_PRS = {
    "interactive #80": ("**Interactive #80** (0.5.47)",),
    "interactive in-silico tier": ("**Interactive in-silico tier** (0.5.48", "`feat/insilico-tier`"),
    "ms-dial predicted-msp semantics": ("**MS-DIAL predicted-MSP semantics**", "`feat/predicted-msp-semantics`"),
    "gate truthful-libraries": ("**Gate truthful-libraries**", "`feat/truthful-libraries-shared-downloads-root`"),
}
_PENDING_MARK = re.compile(r"\*\*\[pending: ([^\]]+)\]\*\*")
# What the contract stated before 2026-10-27 and no longer states: the mandatory D: root, the deletion of the loaded-
# library copy, merges left to Claude, the Console and pull-request heads of 2026-10-09, and the history of the
# question rounds, which the trial manifest keeps.
_WITHDRAWN = (
    "Use this mandatory default repository reanalysis root", "deletes the loaded-library copy",
    "left the merging of the campaign's pull requests", "RawDataHandler 1.3.9776.346", "1681e9a", "e9e5ca9",
    "both open", "second-round answer", "The extra question", "Open for the user", "option A, \"",
)
# Private locations no document may carry: the laboratory NAS and the local library folders. Library file names are
# allowed; their locations are not.
_PRIVATE_LOCATIONS = re.compile(
    r"sysbionas|\\\\[A-Za-z0-9_.-]+\\|libraries[\\/]msp|lbm_versions|Library[\\/]MSP|RawDataHandler-local-feed",
    re.IGNORECASE)
# The decisions from 2026-10-11 to 2026-10-27 the trial manifest records as the user's, each by what it must say.
_DECISIONS_SINCE_2026_10_11 = {
    "2026-10-11": ("ほとんどAで良い", "_library_copies"),
    "2026-10-14": ("Msp20261013094720_NCDK-TUAT-LC25_no-unknown-db.lbm2", "0.1/0.1/0.1"),
    "2026-10-15": ("1つだけで良いと思っています", "いや、いらないです！", "除外で大丈夫です"),
    "2026-10-24": ("一番適切だと考えられることをしてください", "GLACIER", "Lipidomics project", "F:"),
    "2026-10-25": ("0.01 Da", "0.05 Da", "0.5/0.5/0.5", "at least 3 matched peaks",
                   "F:\\MSDIAL_Public_Reanalysis\\analysis"),
    "2026-10-27": ("the user merges the pull requests", "before the production plan"),
}
# The user's own words for the decisions of 2026-10-06 to 2026-10-08, which the trial manifest records.
_WORDS_2026_10_06_TO_08 = {
    "2026-10-06": ("High qualityのMS2を取りたい", "必ず記録として残してください"),
    "2026-10-07": ("無理やりにでも", "まだ公開しませんし、大丈夫です", "マージについては、お任せできますか",
                   "12で確認解析を回してください。#826のマージもOKです。", "推奨でお願いします"),
    "2026-10-08": ("再クロールは必要ないです！",),
}
_ONE_ENCODING_RULE_LABEL = "A: この一つのルールで統一"
# What the trial manifest's 2026-10-03 and 2026-10-02 decisions say, and the user's answers verbatim.
_DECISION_2026_10_03_SAYS = (
    "option A", "unit-level evidence", "is not such evidence", "MTBKS217", "MTBKS219 and MTBKS220",
    "a fourth pause of the whole campaign", "cannot read holds the unit",
    "案AでOK、またパイロット開始もスタートしてください。",
)
_DECISION_2026_10_02_SAYS = (
    "not_evaluable where it is required", "no report", "holds the unit", "technical_settings.ion_mode",
    "'Stop' is per unit", "はい、提案通りで良いです",
)


def _normalised(token: str) -> str:
    name = token.replace("\\", "/").rstrip(".,;")
    if name.startswith("./"):
        name = name[2:]
    return _LINE_REFERENCE.sub("", name).rstrip(":")


def _named_paths(text: str) -> set[str]:
    """Repository-relative paths a document names: in code spans, Markdown links and absolute locations."""
    names = {match.group(1) for match in _ABSOLUTE_ROOT.finditer(text)}
    prose = _FENCE.sub("", text)
    tokens = [words[0] for words in (match.group(1).split() for match in _SPAN.finditer(prose)) if words]
    tokens += [match.group(1) for match in _LINK.finditer(prose)]
    for token in tokens:
        token = _normalised(token)
        if token.split("/", 1)[0] in _REPOSITORY_FOLDERS:
            names.add(token)
    cleaned = set()
    for name in names:
        name = _normalised(name)
        if not any(mark in name for mark in "<>*"):
            cleaned.add(name)
    return cleaned


def _exists_exactly(root: Path, name: str) -> bool:
    """Whether the path exists here with this exact spelling. NTFS matches any case; Linux does not."""
    here = root
    for part in [item for item in name.split("/") if item]:
        try:
            entries = os.listdir(here)
        except OSError:
            return False
        if part not in entries:
            return False
        here = here / part
    return True


def _read(document: str) -> str:
    return (_ROOT / document).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


@functools.cache
def _verifier():
    path = _ROOT / "scripts" / "verify-run-invariants.py"
    spec = importlib.util.spec_from_file_location("verify_run_invariants_contract", path)
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    sys.modules["verify_run_invariants_contract"] = verifier
    spec.loader.exec_module(verifier)
    return verifier


@functools.cache
def _gate_checks_by_stage() -> dict[str, tuple[str, ...]]:
    """The check ids the gate runs at each stage, read from the gate itself on an empty workspace."""
    verifier = _verifier()
    with tempfile.TemporaryDirectory() as scratch:
        return {stage: tuple(check.check_id for check in verifier.verify(Path(scratch), stage).checks)
                for stage in verifier.STAGES}


def _gate_run_policy() -> dict[str, set[str]]:
    verifier = _verifier()
    return {name: {check for check, rule in verifier.RUN_POLICY.items() if rule == name} for name in _GATE_LISTS}


def _runner_module(name: str):
    sys.path.insert(0, str(_ROOT / "scripts"))
    try:
        return importlib.import_module(f"campaign.{name}")
    finally:
        sys.path.remove(str(_ROOT / "scripts"))


def _check_ids(text: str) -> set[str]:
    return set(_CHECK_ID.findall(text)) - _NOT_CHECKS


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = text.find("\n## ", start + len(heading))
    return text[start:] if following < 0 else text[start:following]


def _blocks(text: str) -> list[str]:
    """A document's paragraphs, each list item on its own and on one line, with fenced blocks left out."""
    blocks = []
    for paragraph in re.split(r"\n[ \t]*\n", _FENCE.sub("", text)):
        item: list[str] = []
        for line in paragraph.splitlines():
            if _LIST_ITEM.match(line) and item:
                blocks.append(_flat(" ".join(item)))
                item = []
            item.append(line)
        if item:
            blocks.append(_flat(" ".join(item)))
    return blocks


def _gate_list(section: str, name: str) -> set[str]:
    """The checks a `- `name`:` bullet of the gate rule names, through its continuation lines."""
    lines = section.splitlines()
    marker = f"- `{name}`:"
    starts = [index for index, line in enumerate(lines) if line.startswith(marker)]
    if len(starts) != 1:
        raise AssertionError(f"the gate rule has {len(starts)} bullets for {name}, not one")
    bullet = [lines[starts[0]]]
    for line in lines[starts[0] + 1:]:
        if not line.startswith("  "):
            break
        bullet.append(line)
    return _check_ids("\n".join(bullet))


def _parenthesised_lists(text: str, name: str) -> list[set[str]]:
    """Each list a passage gives in parentheses after "a `name` check", as the batch skill's step 12 does; a
    parenthesis that names no check is no list. A check a pending pull request brings is not part of the list."""
    found = [_check_ids(match.group(1)) - set(PENDING_CHECKS)
             for match in re.finditer(rf"`{name}` check\s*\(([^)]*)\)", text)]
    return [checks for checks in found if checks]


def _decided_lists(decisions: list[dict]) -> dict[str, set[str]]:
    """For each list, the checks named by the latest decision sentence that puts checks in it."""
    found: dict[str, set[str]] = {}
    for entry in decisions:
        for sentence in re.split(r"(?<=[.])\s+", str(entry.get("decision") or "")):
            for name in _GATE_LISTS:
                if f"({name})" in sentence:
                    found[name] = _check_ids(sentence)
    return found


def _latest_list_decision(decisions: list[dict]) -> dict:
    return [entry for entry in decisions
            if any(f"({name})" in str(entry.get("decision") or "") for name in _GATE_LISTS)][-1]


def _trial_decisions() -> list[dict]:
    return json.loads(_read(_TRIAL_MANIFEST))["decisions"]


def _gate_rule_disagreements(contract: str, before_production: set[str], decided: dict[str, set[str]]) -> list[str]:
    """Where the campaign's gate rule departs from the gate or from the user's decision; empty if nowhere."""
    problems = []
    rule = _section(contract, _GATE_RULE_SECTION)
    named = _check_ids(rule) - set(PENDING_CHECKS)
    if named != before_production:
        problems.append(f"the rule names {sorted(named - before_production)} beyond the gate's "
                        f"before-production checks and leaves {sorted(before_production - named)} unnamed")
    lists = {}
    for name in _GATE_LISTS:
        try:
            lists[name] = _gate_list(rule, name)
        except AssertionError as error:
            problems.append(str(error))
            continue
        given = decided.get(name, set())
        if lists[name] != given:
            problems.append(f"{name} holds {sorted(lists[name] - given)} the user did not give it and "
                            f"lacks {sorted(given - lists[name])} the user did")
    if len(lists) == len(_GATE_LISTS) and lists["blocks_run"] & lists["record_only"]:
        problems.append(f"{sorted(lists['blocks_run'] & lists['record_only'])} are in both lists")
    unplaced = before_production - set().union(*decided.values())
    paragraphs = [item for item in re.split(r"\n[ \t]*\n", rule) if item.lstrip().startswith(_NEITHER_LIST)]
    if len(paragraphs) != 1:
        problems.append(f"the rule has {len(paragraphs)} '{_NEITHER_LIST}' paragraphs, not one")
    elif _check_ids(paragraphs[0]) != unplaced:
        problems.append(f"'{_NEITHER_LIST}' names {sorted(_check_ids(paragraphs[0]))}, and the checks "
                        f"the user placed in neither list are {sorted(unplaced)}")
    return problems


def _moved(contract: str, check: str, source: str, target: str) -> str:
    """The contract with `check` taken from the `source` bullet of the gate rule and put at the head of the `target`
    one."""
    rule = _section(contract, _GATE_RULE_SECTION)
    lines = rule.split("\n")
    start = next(index for index, line in enumerate(lines) if line.startswith(f"- `{source}`:"))
    end = start + 1
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    bullet = "\n".join(lines[start:end])
    taken = re.sub(rf"(?:, | and | or ){re.escape(check)}\b|\b{re.escape(check)}(?:, | and )", "", bullet, count=1)
    assert taken != bullet, f"{check} is not in the {source} bullet"
    changed = rule.replace(bullet, taken, 1)
    marker = f"- `{target}`: "
    at = changed.index(marker) + len(marker)
    changed = changed[:at] + check + ", " + changed[at:]
    return contract.replace(rule, changed, 1)


def _case_problems(contract: str) -> list[str]:
    """Where the contract does not state a case the user settled as decided; empty if nowhere."""
    problems = []
    for case, (heading, lead, phrases) in _DECIDED_CASES.items():
        passages = [block for block in _blocks(_section(contract, heading)) if block.startswith(lead)]
        if len(passages) != 1:
            problems.append(f"{case}: {len(passages)} passages open with {lead!r}, not one")
            continue
        missing = [phrase for phrase in phrases if _flat(phrase) not in passages[0]]
        if missing:
            problems.append(f"{case}: the passage does not say {missing}")
    return problems


def _rule_problems(contract: str) -> list[str]:
    """Where the contract does not state a rule the user decided; empty if nowhere."""
    problems = []
    for rule, (heading, phrases) in _RULES.items():
        section = _flat(_section(contract, heading))
        missing = [phrase for phrase in phrases if _flat(phrase) not in section]
        if missing:
            problems.append(f"{rule}: {heading} does not say {missing}")
    return problems


def _still_awaiting(text: str) -> list[str]:
    return [block[:160] for block in _blocks(text) if _AWAITING in block.casefold()]


def _fallback_without_its_field(text: str) -> list[str]:
    """Passages that describe the confirmed=true discard fallback without the policy field that allows it."""
    return [block[:160] for block in _blocks(text)
            if "fallback" in block.casefold() and "confirmed=true" in block and _DISCARD_FALLBACK_FIELD not in block]


def _pending_labels(text: str) -> list[str]:
    return [label.strip().casefold() for match in _PENDING_MARK.finditer(text) for label in match.group(1).split(";")]


class ContractReferencesTests(unittest.TestCase):
    def test_every_repository_path_the_contract_names_exists(self) -> None:
        missing = {}
        for document in DOCUMENTS:
            for name in _named_paths(_read(document)):
                if name not in PENDING and name not in LOCAL_ONLY and not _exists_exactly(_ROOT, name):
                    missing.setdefault(document, []).append(name)
        self.assertEqual({}, missing)

    def test_a_pending_path_is_still_named_and_not_yet_present(self) -> None:
        named = set()
        for document in DOCUMENTS:
            named |= _named_paths(_read(document))
        for name, brought_by in PENDING.items():
            with self.subTest(name=name):
                self.assertIn(name, named, f"{name} is no longer named; drop it from PENDING")
                self.assertFalse((_ROOT / name).exists(), f"{name} exists now ({brought_by} landed); drop it")

    def test_the_reader_finds_names_after_a_fenced_block(self) -> None:
        text = "```text\nD:\\x\n```\n\nRun `scripts/one.py --flag`, then read `trials/two.json`.\n"
        self.assertEqual({"scripts/one.py", "trials/two.json"}, _named_paths(text))

    def test_the_reader_maps_the_absolute_location_and_skips_placeholders(self) -> None:
        text = (
            "Follow `D:\\13_MSDIAL_Public_Reanalysis\\code\\prompts\\01-audit.md`.\n"
            "```powershell\npython D:\\13_MSDIAL_Public_Reanalysis\\code\\scripts\\gate.py `\n```\n"
            "Write `<workspace>\\output\\x.csv` and `tests/<name>.py`, never `output\\method.txt`.\n"
        )
        self.assertEqual({"prompts/01-audit.md", "scripts/gate.py"}, _named_paths(text))

    def test_a_span_broken_across_a_line_keeps_the_names_after_it(self) -> None:
        text = "Record it with `--digest <d> --by <p>\n--conclusion accepted`, then run `scripts/four.py`.\n"
        self.assertEqual({"scripts/four.py"}, _named_paths(text))

    def test_a_line_reference_names_its_file(self) -> None:
        text = "See `scripts/verify-run-invariants.py:2760` and `scripts/record-reading.py:10-20`.\n"
        names = _named_paths(text)
        self.assertEqual({"scripts/verify-run-invariants.py", "scripts/record-reading.py"}, names)
        self.assertTrue(all(_exists_exactly(_ROOT, name) for name in names))

    def test_a_name_resolves_only_in_its_own_case(self) -> None:
        self.assertTrue(_exists_exactly(_ROOT, "scripts/record-reading.py"))
        self.assertFalse(_exists_exactly(_ROOT, "scripts/Record-Reading.PY"))
        self.assertFalse(_exists_exactly(_ROOT, "Scripts/record-reading.py"))

    def test_no_document_names_a_private_location(self) -> None:
        def strings(value) -> list[str]:
            if isinstance(value, dict):
                return [text for item in value.values() for text in strings(item)]
            if isinstance(value, list):
                return [text for item in value for text in strings(item)]
            return [value] if isinstance(value, str) else []

        texts = {document: _read(document) for document in DOCUMENTS}
        # The trial manifest is read as the strings it holds, not as JSON text, whose escaped backslashes would read as
        # network paths.
        texts[_TRIAL_MANIFEST] = "\n".join(strings(json.loads(_read(_TRIAL_MANIFEST))))
        found = {}
        for document, text in texts.items():
            hits = sorted({match.group(0) for match in _PRIVATE_LOCATIONS.finditer(text)})
            if hits:
                found[document] = hits
        self.assertEqual({}, found)
        for planted in ("\\\\sysbionas1\\share\\x.msp", "D:\\x\\libraries\\msp\\VS21.msp", "lbm_versions/a.lbm2"):
            with self.subTest(planted=planted):
                self.assertTrue(_PRIVATE_LOCATIONS.search(f"see {planted} here"))
        self.assertIsNone(_PRIVATE_LOCATIONS.search("`MSMS-Private-pos-VS21.msp` and F:\\MSDIAL_Public_Reanalysis\\analysis"))


class ContractGateChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stages = _gate_checks_by_stage()
        cls.contract = _read(_CONTRACT)

    def _decided(self) -> dict[str, set[str]]:
        decided = _decided_lists(_trial_decisions())
        self.assertEqual(set(_GATE_LISTS), set(decided), "the trial manifest records no decision for a list")
        self.assertTrue(all(decided.values()))
        return decided

    def test_every_check_the_documents_name_is_one_the_gate_runs_or_one_pending(self) -> None:
        runs = {check for checks in self.stages.values() for check in checks}
        unknown = {}
        for document in DOCUMENTS:
            named = _check_ids(_read(document)) - runs - set(PENDING_CHECKS)
            if named:
                unknown[document] = sorted(named)
        self.assertEqual({}, unknown)

    def test_a_pending_check_is_named_as_pending_and_not_yet_run(self) -> None:
        runs = {check for checks in self.stages.values() for check in checks}
        rule = _section(self.contract, _GATE_RULE_SECTION)
        for check, (target, brought_by) in PENDING_CHECKS.items():
            with self.subTest(check=check):
                self.assertNotIn(check, runs, f"the gate runs {check} now ({brought_by} landed): drop it from "
                                              f"PENDING_CHECKS and place it in the {target} bullet")
                passages = [block for block in _blocks(rule) if check in block]
                self.assertTrue(passages, f"the gate rule does not name {check}")
                self.assertTrue(all(_PENDING_MARK.search(block) for block in passages),
                                f"the gate rule names {check} without marking it pending")
                self.assertTrue(any(f"`{target}`" in block for block in passages))
                for name in _GATE_LISTS:
                    self.assertNotIn(check, _gate_list(rule, name), "a pending check is in no list's bullet yet")

    def test_the_campaign_gate_rule_places_every_before_production_check(self) -> None:
        rule = _section(self.contract, _GATE_RULE_SECTION)
        self.assertEqual(set(self.stages["before-production"]), _check_ids(rule) - set(PENDING_CHECKS))

    def test_the_two_lists_hold_exactly_the_checks_the_user_gave_them(self) -> None:
        rule = _section(self.contract, _GATE_RULE_SECTION)
        lists = {name: _gate_list(rule, name) for name in _GATE_LISTS}
        self.assertFalse(lists["blocks_run"] & lists["record_only"], "a check is in both lists")
        decided = self._decided()
        gate = _gate_run_policy()
        for name in _GATE_LISTS:
            with self.subTest(list=name):
                self.assertEqual(decided[name], lists[name], "the contract's list is the latest decision's")
                self.assertEqual(gate[name], lists[name], "the contract's list is the gate's RUN_POLICY's")
                self.assertEqual(_DECIDED_NOW[name], lists[name])

    def test_the_latest_list_decision_is_the_users_of_2026_10_08(self) -> None:
        entry = _latest_list_decision(_trial_decisions())
        self.assertEqual("2026-10-08", entry["at"])
        self.assertTrue(str(entry["by"]).startswith("user"))
        self.assertIn("Answer 1", str(entry["decision"]))
        self.assertEqual(set(self.stages["before-production"]), set().union(*_DECIDED_NOW.values()))

    def test_the_runner_holds_the_same_blocks_run_list(self) -> None:
        policy = _runner_module("policy")
        self.assertEqual(_DECIDED_NOW["blocks_run"], set(policy.BLOCKS_RUN_CHECKS))

    def test_the_batch_skill_gives_the_same_lists(self) -> None:
        skill = _read(_BATCH_SKILL)
        decided = self._decided()
        for name in _GATE_LISTS:
            with self.subTest(list=name):
                given = _parenthesised_lists(skill, name)
                self.assertTrue(given, f"the batch skill gives no {name} list")
                self.assertEqual([decided[name]] * len(given), given)

    def test_the_checks_the_user_placed_in_neither_list_are_named_as_such(self) -> None:
        before_production = set(self.stages["before-production"])
        self.assertEqual([], _gate_rule_disagreements(self.contract, before_production, self._decided()))

    def test_a_check_moved_between_the_lists_is_refused(self) -> None:
        before_production = set(self.stages["before-production"])
        decided = self._decided()
        for check, source, target in (("CONV-1", "blocks_run", "record_only"), ("ID-1", "blocks_run", "record_only"),
                                      ("SPL-1", "record_only", "blocks_run"), ("PKH-1", "record_only", "blocks_run")):
            with self.subTest(check=check, to=target):
                moved = _moved(self.contract, check, source, target)
                problems = _gate_rule_disagreements(moved, before_production, decided)
                self.assertTrue(any(item.startswith(f"{target} holds ['{check}']") for item in problems), problems)
                self.assertTrue(any(item.startswith(f"{source} holds [] ") for item in problems), problems)

    def test_the_rule_is_refused_where_it_departs_from_the_gate_or_the_user(self) -> None:
        section = (
            "## Gate verdicts in a campaign\n\n"
            "- `blocks_run`: ELIG-1 and\n  ACQ-1. Such a unit fails.\n"
            "- `record_only`: CLS-1.\n\n"
            f"{_NEITHER_LIST} SUM-1, and nothing else.\n\n"
            "The runner reads the report.\n"
        )
        gate = {"ELIG-1", "ACQ-1", "CLS-1", "SUM-1"}
        decided = {"blocks_run": {"ELIG-1", "ACQ-1"}, "record_only": {"CLS-1"}}
        self.assertEqual([], _gate_rule_disagreements(section, gate, decided))
        cases = {
            "a check the gate added": (section, gate | {"CNT-1"}, decided),
            "a decided check dropped": (section.replace("ELIG-1 and\n  ", ""), gate, decided),
            "the paragraph deleted": (section.replace(f"{_NEITHER_LIST} SUM-1, and nothing else.\n\n", ""),
                                      gate, decided),
            "an unplaced check in a list": (section.replace("- `record_only`: CLS-1.", "- `record_only`: SUM-1, CLS-1."),
                                            gate, decided),
            "a later decision the rule missed": (section, gate, {**decided, "record_only": {"CLS-1", "SUM-1"}}),
        }
        for case, (text, checks, lists) in cases.items():
            with self.subTest(case=case):
                self.assertTrue(_gate_rule_disagreements(text, checks, lists))

    def test_pre1_reports_no_fail_for_the_rule_to_place(self) -> None:
        verifier = _verifier()
        function = inspect.getsource(verifier.check_preflight_claim)
        reported = re.compile(r'\.add\(\s*"PRE-1"')
        self.assertTrue(reported.findall(function))
        self.assertEqual(len(reported.findall(inspect.getsource(verifier))), len(reported.findall(function)))
        self.assertEqual({"PASS", "WARN", "NOT_EVALUABLE"},
                         set(re.findall(r"\b(PASS|WARN|FAIL|NOT_EVALUABLE)\b", function)))

    def test_a_record_only_fail_hides_an_unevaluated_blocks_run_check_from_the_exit_code(self) -> None:
        """The runner reads the report, not the exit code: a FAIL outranks a strict refusal."""
        verifier = _verifier()
        decided = self._decided()
        with tempfile.TemporaryDirectory() as scratch:
            unit = Path(scratch) / "unit"
            (unit / "provenance").mkdir(parents=True)
            (unit / "output").mkdir()
            (unit / "provenance" / "run-manifest.json").write_text(json.dumps({
                "schema": "msdial-public-reanalysis-run.v1",
                "project": {"analysis_unit_id": "unit"},
                "execution_allowed": True,
            }), encoding="utf-8")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = verifier.main([str(unit), "--stage", "before-production", "--strict", "--json"])
        report = json.loads(printed.getvalue())
        fails = {check["check_id"] for check in report["checks"] if check["status"] == verifier.FAIL}
        self.assertEqual(2, code)
        self.assertTrue(fails)
        self.assertLessEqual(fails, decided["record_only"])
        unevaluated = set(report["strict_failures"]) & decided["blocks_run"]
        self.assertTrue(unevaluated, report["strict_failures"])
        self.assertEqual(unevaluated, set(report["run_blocked_by"]))


class ContractRulesTests(unittest.TestCase):
    """Each rule the user decided is stated in the contract, as the rules in force, and nothing withdrawn remains."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _read(_CONTRACT)

    def test_every_case_the_user_settled_is_stated_as_decided(self) -> None:
        self.assertEqual([], _case_problems(self.contract))

    def test_every_rule_the_user_decided_is_stated(self) -> None:
        self.assertEqual([], _rule_problems(self.contract))

    def test_a_rule_taken_out_is_refused(self) -> None:
        for rule, (heading, phrases) in _RULES.items():
            with self.subTest(rule=rule):
                section = _section(self.contract, heading)
                phrase = phrases[0]
                flat_section = _flat(section)
                self.assertIn(_flat(phrase), flat_section)
                problems = _rule_problems(self.contract.replace(section, flat_section.replace(_flat(phrase), "")))
                self.assertTrue(any(item.startswith(f"{rule}:") for item in problems), problems)

    def test_a_case_stated_as_an_earlier_draft_read_it_is_refused(self) -> None:
        readings = {
            "no report": ("holds the unit", "stops the run"),
            "not evaluable": ("required stops the run exactly as its FAIL does", "is recorded with the unit"),
            "mzXML": ("exactly one polarity", "no polarity at all"),
            "a stop is per unit": ("never the runner", "and the campaign stops"),
            "ion mobility from unit-level evidence": ("is not such evidence", "is evidence too"),
            "declared inputs partly openable": ("is excluded whole", "is held"),
        }
        for case, (decided, earlier) in readings.items():
            with self.subTest(case=case):
                heading, _lead, _phrases = _DECIDED_CASES[case]
                section = _section(self.contract, heading)
                earlier_section = re.sub(r"\s+".join(map(re.escape, decided.split())), earlier, section)
                self.assertNotEqual(section, earlier_section)
                problems = _case_problems(self.contract.replace(section, earlier_section))
                self.assertTrue(any(item.startswith(f"{case}:") for item in problems), problems)

    def test_no_document_marks_a_case_as_still_awaiting_the_user(self) -> None:
        found = {document: _still_awaiting(_read(document)) for document in DOCUMENTS if _still_awaiting(_read(document))}
        self.assertEqual({}, found)

    def test_what_the_contract_no_longer_states_is_gone(self) -> None:
        flat = _flat(self.contract)
        for phrase in _WITHDRAWN:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, flat)

    def test_the_contract_points_to_the_decision_history(self) -> None:
        opening = _flat(self.contract[:self.contract.index("## Mission")])
        self.assertIn(f"`{_TRIAL_MANIFEST}`", opening)
        self.assertIn("This file states the rules in force", opening)

    def test_every_pending_mark_names_a_listed_pull_request(self) -> None:
        labels = _pending_labels(self.contract)
        self.assertTrue(labels)
        self.assertEqual(set(), set(labels) - set(_PENDING_PRS), "a mark names a pull request the list lacks")
        self.assertEqual(set(), set(_PENDING_PRS) - set(labels), "a listed pull request no rule rests on")
        pending = _flat(_section(self.contract, _PENDING_SECTION))
        for label, phrases in _PENDING_PRS.items():
            for phrase in phrases:
                with self.subTest(label=label, phrase=phrase):
                    self.assertIn(phrase, pending)

    def test_the_rules_that_rest_on_unmerged_code_are_marked_pending(self) -> None:
        """Each rule the merged code does not yet carry is marked where it is stated, with the pull request it waits on."""
        expectations = {
            _STORAGE_SECTION: ("gate truthful-libraries",),
            _ANNOTATION_SECTION: ("interactive #80", "interactive in-silico tier", "ms-dial predicted-msp semantics",
                                  "gate truthful-libraries"),
            _RETENTION_SECTION: ("gate truthful-libraries",),
            _GATE_RULE_SECTION: ("gate truthful-libraries",),
        }
        for heading, labels in expectations.items():
            marked = set(_pending_labels(_section(self.contract, heading)))
            for label in labels:
                with self.subTest(section=heading, label=label):
                    self.assertIn(label, marked)

    def test_the_approval_covers_the_boundaries_the_runner_records(self) -> None:
        ledger = _runner_module("ledger")
        self.assertEqual(("1", "3", "4", "5", "split"), ledger.BOUNDARIES)
        source = _read("scripts/campaign-runner.py")
        self.assertIn('covers = ["1", "3", "4", "split"] + (["5"] if args.retention == "delete_after_validated_output"',
                      source)

    def test_every_description_of_the_discard_fallback_names_its_policy_field(self) -> None:
        found = {document: _fallback_without_its_field(_read(document))
                 for document in DOCUMENTS if _fallback_without_its_field(_read(document))}
        self.assertEqual({}, found)
        bare = self.contract.replace(f"`{_DISCARD_FALLBACK_FIELD}: true`", "an approval")
        self.assertEqual(1, len(_fallback_without_its_field(bare)))

    def test_the_go_signal_is_the_users_in_every_document_that_runs_a_campaign(self) -> None:
        words = "本番開始前には私がGoサインを出すので、聞いてください。"
        self.assertIn(words, self.contract)
        entries = [entry for entry in _trial_decisions() if words in str(entry["decision"])]
        self.assertEqual(1, len(entries))
        self.assertEqual("2026-10-01", entries[0]["at"])
        self.assertTrue(str(entries[0]["by"]).startswith("user"))
        skill = _flat(_read(_BATCH_SKILL))
        self.assertIn(words, skill)
        self.assertIn("never started or scheduled on the production campaign before the user's explicit go", skill)
        self.assertIn("Nor is it the user's go", _flat(_read("README.md")))

    def test_the_skill_and_readme_state_the_root_and_the_approval_as_the_contract_does(self) -> None:
        skill = _flat(_read(_BATCH_SKILL))
        readme = _flat(_read("README.md"))
        for text, phrases in ((skill, ("F:\\MSDIAL_Public_Reanalysis\\analysis", "archive of the pilots and preflights",
                                       "`scripts/campaign-runner.py approve`", "with no per-unit confirmation",
                                       "`_library_copies`")),
                              (readme, ("F:\\MSDIAL_Public_Reanalysis\\analysis",
                                        "Inside a campaign there are no per-unit confirmations",
                                        "`scripts/campaign-runner.py approve`"))):
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertIn(phrase, text)
        self.assertNotIn("_Loaded.msp2.dbs` is deleted", skill)


class StopIsPerUnitTests(unittest.TestCase):
    """What the contract says a stop is, held to the runner the campaign runs (scripts/campaign)."""

    def setUp(self) -> None:
        self.contract = _read(_CONTRACT)
        heading, lead, _phrases = _DECIDED_CASES["a stop is per unit"]
        self.passage = next(block for block in _blocks(_section(self.contract, heading)) if block.startswith(lead))

    def test_every_held_state_of_the_runner_is_named_in_the_gate_rule(self) -> None:
        ledger = _runner_module("ledger")
        section = _section(self.contract, _GATE_RULE_SECTION)
        self.assertEqual(("gate_held", "contract_held"), ledger.HELD_STATES)
        for state in (*ledger.HELD_STATES, ledger.DISPOSITION_HELD):
            with self.subTest(state=state):
                self.assertIn(f"`{state}`", section)

    def test_the_runners_four_campaign_pauses_are_the_ones_named(self) -> None:
        policy = _runner_module("policy")
        self.assertEqual({"pin", "disk", "outage", "backend"}, set(policy.CAMPAIGN_PAUSES))
        for kind, name in policy.CAMPAIGN_PAUSES.items():
            with self.subTest(kind=kind):
                self.assertIn(name.split(" ", 1)[1], self.passage)

    def test_no_pause_waits_for_an_operator(self) -> None:
        for earlier in ("until an operator resumes", "only an operator lifts", "waits for an operator"):
            with self.subTest(earlier=earlier):
                self.assertNotIn(earlier, self.passage)
                put_back = self.contract.replace("Nothing one unit does pauses the campaign",
                                                 f"A broken contract pauses the campaign {earlier}")
                self.assertTrue(any(item.startswith("a stop is per unit:") for item in _case_problems(put_back)))


class TrialDecisionsTests(unittest.TestCase):
    """The trial manifest keeps the history of the decisions, appended as they are taken."""

    def test_decisions_are_dated_and_in_order(self) -> None:
        decisions = _trial_decisions()
        for entry in decisions:
            with self.subTest(entry=entry.get("decision", "")[:60]):
                self.assertRegex(entry["at"], r"^\d{4}-\d{2}-\d{2}")
                self.assertTrue(str(entry["decision"]).strip())
        dates = [entry["at"][:10] for entry in decisions]
        self.assertEqual(sorted(dates), dates)

    def test_the_2026_10_02_and_10_03_decisions_are_recorded_in_the_users_words(self) -> None:
        decisions = _trial_decisions()
        october_2 = [entry for entry in decisions
                     if str(entry.get("by", "")).startswith("user") and entry["at"].startswith("2026-10-02")]
        decision = str(_latest_list_decision(october_2)["decision"])
        for phrase in _DECISION_2026_10_02_SAYS:
            with self.subTest(day="2026-10-02", phrase=phrase):
                self.assertIn(phrase, decision)
        october_3 = [entry for entry in decisions if entry["at"].startswith("2026-10-03")]
        self.assertEqual(1, len(october_3))
        for phrase in _DECISION_2026_10_03_SAYS:
            with self.subTest(day="2026-10-03", phrase=phrase):
                self.assertIn(phrase, str(october_3[0]["decision"]))

    def test_the_decisions_of_2026_10_06_to_09_are_recorded_in_the_users_words(self) -> None:
        decisions = _trial_decisions()
        for day, words in _WORDS_2026_10_06_TO_08.items():
            entries = [entry for entry in decisions
                       if entry["at"].startswith(day) and str(entry.get("by", "")).startswith("user")]
            for phrase in words:
                with self.subTest(day=day, phrase=phrase):
                    self.assertEqual(1, len([entry for entry in entries if phrase in str(entry["decision"])]))
        rule = [entry for entry in decisions if _ONE_ENCODING_RULE_LABEL in str(entry["decision"])]
        self.assertEqual(1, len(rule))
        self.assertEqual("2026-10-09", rule[0]["at"])

    def test_the_decisions_since_2026_10_11_are_recorded_as_the_users(self) -> None:
        decisions = _trial_decisions()
        for day, phrases in _DECISIONS_SINCE_2026_10_11.items():
            entries = [entry for entry in decisions
                       if entry["at"] == day and str(entry.get("by", "")).startswith("user")]
            with self.subTest(day=day):
                self.assertEqual(1, len(entries))
            for phrase in phrases:
                with self.subTest(day=day, phrase=phrase):
                    self.assertIn(phrase, str(entries[0]["decision"]))

    def test_the_rules_replaced_since_are_marked_where_they_were_recorded(self) -> None:
        marks = {
            "Before production, in the campaign: a FAIL stops a unit's MS-DIAL run only for": "Replaced on 2026-10-02",
            "mzXML conversion, in the campaign, as relayed with the campaign rules on 2026-10-01":
                "Replaced on 2026-10-02",
            "An AIF file whose collision-energy target list is empty": "Replaced on 2026-10-08 by the user's answer 3",
            "Raw data, in the campaign:": "Replaced in part on 2026-10-11",
            "Libraries for the campaign:": "The LBM2 was replaced on 2026-10-14",
        }
        decisions = _trial_decisions()
        for opening, mark in marks.items():
            with self.subTest(opening=opening):
                entries = [entry for entry in decisions if str(entry["decision"]).startswith(opening)]
                self.assertEqual(1, len(entries))
                self.assertIn(mark, str(entries[0].get("result")))
        merges = [entry for entry in decisions if "マージについては、お任せできますか" in str(entry["decision"])]
        self.assertEqual(1, len(merges))
        self.assertIn("(replaced on 2026-10-27: the user merges)", str(merges[0]["decision"]))


if __name__ == "__main__":
    unittest.main()
