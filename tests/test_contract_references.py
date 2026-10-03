"""The contract names files and gate checks in this repository, and each name must resolve in this tree.

CLAUDE.md, README.md, AGENTS.md and the project skills tell a session which script to run, which
record to read and which template to return. A name that resolves to nothing sends that session
looking for a file that is not there, or lets it conclude that a step does not exist. Locations
outside this repository (the Interactive checkout, the analysis root) are not checked: they are not
this tree's to hold, and they differ from machine to machine.

A document may name a file that another change is bringing, as the campaign amendment names the
campaign runner before it lands. Such a name is listed in PENDING with what brings it, and the test
fails once the file exists, so that the entry is removed rather than left to excuse a later broken
name.

The documents also name gate checks, and the campaign amendment decides by check which
before-production FAILs stop a unit's run (user decisions, 2026-10-01 and 2026-10-02). A check named
there that the gate does not run is a rule nobody applies, and a before-production check the rule
leaves out is one whose FAIL nobody has placed. So every named check must be one the gate runs, the
campaign's gate rule must place every before-production check the gate runs, and the two lists must
hold exactly the checks the trial manifest's latest decision gives them and the gate's RUN_POLICY
states. Since 2026-10-02 the user has placed every check: blocks_run holds ELIG-1, ACQ-1, SUM-1,
CNT-1, INP-1, ID-1, PRE-2 and CONV-1, record_only CLS-1, CLS-2, CLS-3, ORD-1, PKH-1, SPL-1 and PRE-1,
and the paragraph that names the checks in neither list names none. A list may not gain or lose a
check in either direction: one moved to record_only is a FAIL that no longer stops a run, and one
moved to blocks_run is a unit failed and its raw data deleted, and neither is the contract's to decide.

The same decision settled the cases the rule of 2026-10-01 left open, and the contract states each as
decided, with no passage still marked as awaiting the user: a blocks_run check left not_evaluable where
it is required stops the run; a gate that gives no usable report holds the unit, unrun and uncounted
with its raw data kept, and after the run is only recorded; an mzXML without a polarity is given its
unit's one declared ion mode, and CONV-1 stops the unit where there is none; and a stop is the unit's,
never the runner's. The contract states what the runner does under that last rule: a held unit keeps
run --until-idle going for its rechecks; a reply of Interactive's the runner cannot read for one unit, a
reply that does not parse among them, holds that unit (contract_held), as a missing report does, and
pauses nothing; and only what every unit meets alike pauses the campaign, in the four pauses the runner
names, each lifting by itself. Every passage that describes the confirmed=true discard fallback names the
policy field that carries the user's approval of it.

The decision of 2026-10-03 settled three more cases, and the contract states each as decided: a unit
counts as ion mobility only on its own evidence, a mixed unit passing to the raw-header check (option A);
an Interactive backend that does not answer is the fourth pause and a reply the runner cannot read holds
the unit, the two defaults the user did not object to; and an mzXML whose scans mix the opposite polarity
with scans of none is excluded while the rest of its unit runs. The trial manifest records that decision
with the user's answer verbatim. As the review of ba035ca made the runner and the plan hold to it, the
contract also says that the backend pause covers every call, the diagnostic's estimate as much as a job's
poll; that an unreadable poll is never taken for a lost job, the unit waiting for its Console and then held
at that poll; and that a unit whose Catalog record the plan cannot read is never excluded for ion mobility
on the plan's own row.
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
_BATCH_SKILL = ".claude/skills/msdial-repository-batch/SKILL.md"
# The top-level folders of this repository. A code span whose first segment is one of them names a
# file here; any other span (a workspace path, a manifest field, an argument) is not a claim about
# this tree.
_REPOSITORY_FOLDERS = frozenset({".claude", "feedback", "prompts", "scripts", "tests", "trials"})
# The documents also name this repository by its absolute location on the author's machine, with
# either separator.
_ABSOLUTE_ROOT = re.compile(r"D:[\\/]13_MSDIAL_Public_Reanalysis[\\/]code[\\/]([^\s`)'\"]+)", re.IGNORECASE)
_FENCE = re.compile(r"^```.*?^```[^\n]*$", re.MULTILINE | re.DOTALL)
# A code span may break across a line, as Markdown lets it, but never across a blank line. Read
# line by line, the span's closing backtick pairs with the next span's opening one, and every name
# after it in the paragraph is read as prose.
_SPAN = re.compile(r"`((?:[^`\n]|\n(?![ \t]*\n))+)`")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
# A file:line reference (scripts/x.py:2760, or :10-20) names the file; Windows would read the
# colon as the start of an alternate data stream name.
_LINE_REFERENCE = re.compile(r":\d+(?:-\d+)?$")

PENDING: dict[str, str] = {}
# Named on purpose and kept out of the repository by .gitignore: audit outputs under feedback/ are
# local by default, and only the review template is version controlled (README).
LOCAL_ONLY = frozenset({"feedback/codex-pre-audit-2026-09-02.md"})

# A gate check id: letters, a hyphen and one digit (SUM-1, CONV-1). Tokens of that shape that are
# not checks are listed here.
_CHECK_ID = re.compile(r"\b[A-Z]{2,5}-[1-9]\b")
_NOT_CHECKS = frozenset({"UTF-8"})
_GATE_RULE_SECTION = "## Gate verdicts in a campaign"
_SCOPE_SECTION = "## Supported production scope"
_GATE_LISTS = ("blocks_run", "record_only")
# The lists as the user gave them on 2026-10-02, written out once more so that a later decision changes
# this test as well as the trial manifest: a list cannot move without the change saying so.
_DECIDED_2026_10_02 = {
    "blocks_run": {"ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1", "ID-1", "PRE-2", "CONV-1"},
    "record_only": {"CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1", "SPL-1", "PRE-1"},
}
# The paragraph of the gate rule that names the before-production checks the user placed in neither
# list: none since 2026-10-02, and any the gate adds until the user places it.
_NEITHER_LIST = "**In neither list.**"
# What marked a passage as the contract's reading of a case the user had not settled. Since 2026-10-02
# no case is open, and no passage carries it. The discard fallback is marked "awaiting the user's
# approval" instead, and named by its policy field.
_AWAITING = "awaiting the user's decision"
# What each case the user settled on 2026-10-02 is stated as: the lead of the passage that states it, the
# section it is in, and what the passage must say. Each lead is the passage's opening words.
_DECIDED_CASES = {
    "not evaluable": (_GATE_RULE_SECTION, "**Not evaluable.**", (
        "`blocks_run` check left `not_evaluable` where it is", "required stops the run exactly as its FAIL does",
        "`strict_failures`", "not required never stops the run",
    )),
    "no report": (_GATE_RULE_SECTION, "**No report.**", (
        "holds the unit", "keeps its raw data", "counted neither as a retry nor as a failure", "`gate_held`",
        "every other unit goes on", "runs the gate again", "`scripts/campaign-runner.py recheck-held`",
        "a missing report is only recorded",
        # A held unit is not idle, so the rechecks every few hours come by themselves; a request waits for a
        # runner, and the command says when none holds the campaign.
        "`run --until-idle`", "does not return while a unit is held", "one must be started",
    )),
    "a stop is per unit": (_GATE_RULE_SECTION, "**A stop is per unit.**", (
        "never the runner", "pause the whole campaign",
        # The four pauses the runner makes, each lifting by itself, the backend's the user's default of 2026-10-03.
        "A pin change, a short disk, a repository outage and an Interactive backend that does not answer",
        "the four pauses the status export names", "lifts by itself", "did not object to on 2026-10-03",
        "times out", "none of these counts against the unit",
        # Whichever call goes unanswered: the review of ba035ca found the estimate counted against the unit.
        "a job's poll or the diagnostic's estimate",
        # One unit's record holds that unit, a reply that does not parse among them; it pauses nothing.
        "Nothing one unit does pauses the campaign", "`contract_held`", "a reply that does not parse",
        "counted neither as a retry nor as a failure", "leaves that unit's raw data held",
        # An unreadable poll is not a lost job, which started a second production Console on the unit.
        "never taken for a lost job", "keeping the Console slot", "held at that poll",
    )),
    "mzXML without a polarity": (_SCOPE_SECTION, "- **mzXML without a polarity.**", (
        "exactly one polarity", "`technical_settings.ion_mode`", "imputed from that", "as an inference",
        "nothing is imputed", "CONV-1 FAILs", "`blocks_run` check", "no scan of the opposite polarity",
    )),
    # Settled on 2026-10-03.
    "ion mobility from unit-level evidence": (_SCOPE_SECTION, "- **Ion mobility from unit-level evidence.**", (
        "2026-10-03", "option A", "only on its own evidence", "study-level text", "is not such evidence",
        "MTBKS217", "`ion_mobility_evidence` state `enabled`", "A mixed unit", "MTBKS219 and MTBKS220",
        "is not excluded at plan time", "passes to the raw-header check", "split",
        # A record the plan cannot read is no evidence: its own row holds neither rows nor inputs.
        "whose Catalog record the plan cannot read", "never excluded for ion mobility on that row",
    )),
    "mzXML with scans of both polarities": (_SCOPE_SECTION, "- **mzXML with scans of both polarities.**", (
        "did not object to on 2026-10-03", "declares one polarity", "opposite polarity", "record none",
        "excluded with its reason recorded", "the rest of the unit runs", "not given the declared one",
    )),
}
# What the trial manifest's 2026-10-03 decision says, and the user's answer verbatim.
_DECISION_2026_10_03_SAYS = (
    "option A", "unit-level evidence", "is not such evidence", "MTBKS217", "MTBKS219 and MTBKS220",
    "15 named analysis units, raw data kept", "a fourth pause of the whole campaign",
    "cannot read holds the unit", "mix the opposite polarity with scans of no polarity is excluded",
    "\u6848A\u3067OK\u3001\u307e\u305f\u30d1\u30a4\u30ed\u30c3\u30c8\u958b\u59cb\u3082"
    "\u30b9\u30bf\u30fc\u30c8\u3057\u3066\u304f\u3060\u3055\u3044\u3002",
)
# What the trial manifest's 2026-10-02 decision says of the same cases, and the user's answer verbatim.
_DECISION_SAYS = (
    "not_evaluable where it is required", "no report", "holds the unit", "technical_settings.ion_mode",
    "'Stop' is per unit", "\u306f\u3044\u3001\u63d0\u6848\u901a\u308a\u3067\u826f\u3044\u3067\u3059",
)
_DISCARD_FALLBACK_FIELD = "confirmed_discard_fallback"
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*]|\d+\.)[ \t]")


def _normalised(token: str) -> str:
    name = token.replace("\\", "/").rstrip(".,;")
    if name.startswith("./"):
        name = name[2:]
    return _LINE_REFERENCE.sub("", name).rstrip(":")


def _named_paths(text: str) -> set[str]:
    """Repository-relative paths a document names: in code spans, Markdown links and absolute locations."""
    names = {match.group(1) for match in _ABSOLUTE_ROOT.finditer(text)}
    # A fenced block is removed before spans are paired: its three backticks would otherwise shift
    # every pairing after it, and the spans read would be the prose between them.
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


@functools.cache
def _verifier():
    path = _ROOT / "scripts" / "verify-run-invariants.py"
    spec = importlib.util.spec_from_file_location("verify_run_invariants_contract", path)
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    sys.modules["verify_run_invariants_contract"] = verifier
    spec.loader.exec_module(verifier)
    return verifier


def _gate_checks_by_stage() -> dict[str, list[str]]:
    """The check ids the gate runs at each stage, read from the gate itself on an empty workspace.

    Every check reports on a workspace with no artifacts (not_evaluable, never skipped), so this is
    the gate's own list, not one written down beside it.
    """
    verifier = _verifier()
    with tempfile.TemporaryDirectory() as scratch:
        return {stage: [check.check_id for check in verifier.verify(Path(scratch), stage).checks]
                for stage in verifier.STAGES}


def _gate_run_policy() -> dict[str, set[str]]:
    """The two lists as the gate's RUN_POLICY states them, the table its --json run_policy is read from."""
    verifier = _verifier()
    return {name: {check for check, rule in verifier.RUN_POLICY.items() if rule == name} for name in _GATE_LISTS}


def _check_ids(text: str) -> set[str]:
    return set(_CHECK_ID.findall(text)) - _NOT_CHECKS


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = text.find("\n## ", start + len(heading))
    return text[start:] if following < 0 else text[start:following]


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
    """Each list a passage gives in parentheses after "a `name` check", as the batch skill's step 12 does. A
    parenthesis that names no check (a date, a reference) is no list."""
    found = [_check_ids(match.group(1)) for match in re.finditer(rf"`{name}` check\s*\(([^)]*)\)", text)]
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
    """The latest decision that puts checks in a list."""
    return [entry for entry in decisions if any(f"({name})" in str(entry.get("decision") or "") for name in _GATE_LISTS)][-1]


def _trial_decisions() -> list[dict]:
    return json.loads(
        (_ROOT / "trials" / "2026-09-20-ten-unit-trial-manifest.json").read_text(encoding="utf-8")
    )["decisions"]


def _gate_rule_disagreements(
    contract: str, before_production: set[str], decided: dict[str, set[str]],
) -> list[str]:
    """Where the campaign's gate rule departs from the gate or from the user's decision; empty if nowhere."""
    problems = []
    rule = _section(contract, _GATE_RULE_SECTION)
    named = _check_ids(rule)
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
    paragraphs = [item for item in re.split(r"\n[ \t]*\n", rule)
                  if item.lstrip().startswith(_NEITHER_LIST)]
    if len(paragraphs) != 1:
        problems.append(f"the rule has {len(paragraphs)} '{_NEITHER_LIST}' paragraphs, not one")
    elif _check_ids(paragraphs[0]) != unplaced:
        problems.append(f"'{_NEITHER_LIST}' names {sorted(_check_ids(paragraphs[0]))}, and the checks "
                        f"the user placed in neither list are {sorted(unplaced)}")
    return problems


def _moved(contract: str, check: str, source: str, target: str) -> str:
    """The contract with `check` taken from the `source` bullet of the gate rule and put at the head of
    the `target` one."""
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


def _blocks(text: str) -> list[str]:
    """A document's paragraphs, each list item on its own and on one line, with fenced blocks left out."""
    blocks = []
    for paragraph in re.split(r"\n[ \t]*\n", _FENCE.sub("", text)):
        item: list[str] = []
        for line in paragraph.splitlines():
            if _LIST_ITEM.match(line) and item:
                blocks.append(" ".join(" ".join(item).split()))
                item = []
            item.append(line)
        if item:
            blocks.append(" ".join(" ".join(item).split()))
    return blocks


def _case_problems(contract: str) -> list[str]:
    """Where the contract does not state a case the user settled (2026-10-02, 2026-10-03) as decided; empty if
    nowhere."""
    problems = []
    for case, (heading, lead, phrases) in _DECIDED_CASES.items():
        passages = [block for block in _blocks(_section(contract, heading)) if block.startswith(lead)]
        if len(passages) != 1:
            problems.append(f"{case}: {len(passages)} passages open with {lead!r}, not one")
            continue
        missing = [phrase for phrase in phrases if " ".join(phrase.split()) not in passages[0]]
        if missing:
            problems.append(f"{case}: the passage does not say {missing}")
    return problems


def _still_awaiting(text: str) -> list[str]:
    """Passages that still mark a case as awaiting the user's decision."""
    return [block[:160] for block in _blocks(text) if _AWAITING in block.casefold()]


def _fallback_without_its_field(text: str) -> list[str]:
    """Passages that describe the confirmed=true discard fallback without the policy field that allows it."""
    return [block[:160] for block in _blocks(text)
            if "fallback" in block and "confirmed=true" in block and _DISCARD_FALLBACK_FIELD not in block]


class ContractReferencesTests(unittest.TestCase):
    def test_every_repository_path_the_contract_names_exists(self) -> None:
        missing = {}
        for document in DOCUMENTS:
            text = (_ROOT / document).read_text(encoding="utf-8")
            for name in _named_paths(text):
                if name not in PENDING and name not in LOCAL_ONLY and not _exists_exactly(_ROOT, name):
                    missing.setdefault(document, []).append(name)
        self.assertEqual({}, missing)

    def test_a_pending_path_is_still_named_and_not_yet_present(self) -> None:
        named = set()
        for document in DOCUMENTS:
            named |= _named_paths((_ROOT / document).read_text(encoding="utf-8"))
        for name, brought_by in PENDING.items():
            with self.subTest(name=name):
                self.assertIn(name, named, f"{name} is no longer named; drop it from PENDING")
                self.assertFalse(
                    (_ROOT / name).exists(),
                    f"{name} exists now ({brought_by} landed); drop it from PENDING",
                )

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

    def test_the_reader_finds_the_names_a_review_showed_it_missing(self) -> None:
        text = (
            "Run `./scripts/one.py`, read [the trial](trials/two.json) and\n"
            "`D:/13_MSDIAL_Public_Reanalysis/code/prompts/three.md`.\n"
        )
        self.assertEqual({"scripts/one.py", "trials/two.json", "prompts/three.md"}, _named_paths(text))

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


class ContractGateChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stages = _gate_checks_by_stage()
        cls.contract = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    def test_every_check_the_documents_name_is_one_the_gate_runs(self) -> None:
        runs = {check for checks in self.stages.values() for check in checks}
        unknown = {}
        for document in DOCUMENTS:
            named = _check_ids((_ROOT / document).read_text(encoding="utf-8")) - runs
            if named:
                unknown[document] = sorted(named)
        self.assertEqual({}, unknown)

    def _decided(self) -> dict[str, set[str]]:
        decided = _decided_lists(_trial_decisions())
        self.assertEqual(set(_GATE_LISTS), set(decided), "the trial manifest records no decision for a list")
        self.assertTrue(all(decided.values()))
        return decided

    def test_the_campaign_gate_rule_places_every_before_production_check(self) -> None:
        rule = _section(self.contract, _GATE_RULE_SECTION)
        self.assertEqual(set(self.stages["before-production"]), _check_ids(rule))

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

    def test_the_lists_are_the_ones_the_user_gave_on_2026_10_02(self) -> None:
        self.assertEqual(_DECIDED_2026_10_02, self._decided())
        self.assertEqual(_DECIDED_2026_10_02, _gate_run_policy())
        self.assertEqual(set(self.stages["before-production"]), set().union(*_DECIDED_2026_10_02.values()),
                         "every before-production check the gate runs is placed")

    def test_the_batch_skill_gives_the_same_lists(self) -> None:
        skill = (_ROOT / _BATCH_SKILL).read_text(encoding="utf-8")
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
        # Moved to record_only, its FAIL would stop no run; moved to blocks_run, each FAIL would fail the
        # unit and delete its raw data. The user decided neither.
        for check, source, target in (("CONV-1", "blocks_run", "record_only"), ("ID-1", "blocks_run", "record_only"),
                                      ("SPL-1", "record_only", "blocks_run"), ("PKH-1", "record_only", "blocks_run")):
            with self.subTest(check=check, to=target):
                moved = _moved(self.contract, check, source, target)
                self.assertEqual(before_production, _check_ids(_section(moved, _GATE_RULE_SECTION)))
                problems = _gate_rule_disagreements(moved, before_production, decided)
                self.assertTrue(any(item.startswith(f"{target} holds ['{check}']") for item in problems), problems)
                self.assertTrue(any(item.startswith(f"{source} holds [] ") for item in problems), problems)

    def test_pre1_reports_no_fail_for_the_rule_to_place(self) -> None:
        # The rule says PRE-1 never FAILs. It is reported in one function, which names no FAIL.
        verifier = _verifier()
        function = inspect.getsource(verifier.check_preflight_claim)
        reported = re.compile(r'\.add\(\s*"PRE-1"')
        self.assertTrue(reported.findall(function))
        self.assertEqual(len(reported.findall(inspect.getsource(verifier))), len(reported.findall(function)))
        self.assertEqual({"PASS", "WARN", "NOT_EVALUABLE"},
                         set(re.findall(r"\b(PASS|WARN|FAIL|NOT_EVALUABLE)\b", function)))

    def test_a_record_only_fail_hides_an_unevaluated_blocks_run_check_from_the_exit_code(self) -> None:
        # The rule says the runner reads the report, not the exit code, because a FAIL outranks a strict
        # refusal. A production bundle with no peak-height diagnostic FAILs PKH-1 (record_only), and the
        # blocks_run checks whose artifacts are missing are left not_evaluable beside it: exit 2, not 4.
        # Those stop the run all the same (2026-10-02), and the report's run_blocked_by names them.
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
        self.assertEqual(unevaluated, set(report["run_blocked_by"]), "the unevaluated blocks_run checks stop the run")

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
            "the paragraph deleted": (section.replace(f"{_NEITHER_LIST} SUM-1, and nothing else.\n\n", ""), gate, decided),
            "an unplaced check in a list": (section.replace("- `record_only`: CLS-1.", "- `record_only`: SUM-1, CLS-1."),
                                            gate, decided),
            "a placed check called unplaced": (section.replace("SUM-1, and", "SUM-1 and CLS-1, and"),
                                               gate, decided),
            "a later decision the rule missed": (section, gate,
                                                 {**decided, "record_only": {"CLS-1", "SUM-1"}}),
        }
        for case, (text, checks, lists) in cases.items():
            with self.subTest(case=case):
                self.assertTrue(_gate_rule_disagreements(text, checks, lists))

    def test_every_case_the_user_settled_is_stated_as_decided(self) -> None:
        self.assertEqual([], _case_problems(self.contract))

    def test_no_document_marks_a_case_as_still_awaiting_the_user(self) -> None:
        found = {}
        for document in DOCUMENTS:
            text = (_ROOT / document).read_text(encoding="utf-8")
            if _still_awaiting(text):
                found[document] = _still_awaiting(text)
        self.assertEqual({}, found)

    def test_a_case_stated_as_the_draft_read_it_is_refused(self) -> None:
        # Before 2026-10-02 the draft read the open cases otherwise: a gate with no report stopped the run as a
        # failed attempt, a check left not_evaluable was recorded whatever its list, and an mzXML's polarity
        # was never imputed. Each reading put back is refused, and so is a passage put back under its mark.
        readings = {
            "no report": ("holds the unit", "stops the run"),
            "not evaluable": ("required stops the run exactly as its FAIL does", "is recorded with the unit"),
            "mzXML without a polarity": ("exactly one polarity", "no polarity at all"),
            "a stop is per unit": ("never the runner", "and the campaign stops"),
        }
        for case, (decided, earlier) in readings.items():
            with self.subTest(case=case):
                heading, lead, _phrases = _DECIDED_CASES[case]
                self.assertIn(decided, " ".join(_section(self.contract, heading).split()))
                section = _section(self.contract, heading)
                earlier_section = re.sub(r"\s+".join(map(re.escape, decided.split())), earlier, section)
                self.assertNotEqual(section, earlier_section)
                problems = _case_problems(self.contract.replace(section, earlier_section))
                self.assertTrue(any(item.startswith(f"{case}:") for item in problems), problems)
        marked = self.contract.replace("**No report.**", f"**No report, {_AWAITING}.**")
        self.assertTrue(_still_awaiting(marked))
        self.assertTrue(any(item.startswith("no report:") for item in _case_problems(marked)))

    def test_the_latest_decision_settles_the_cases_in_the_users_words(self) -> None:
        decision = str(_latest_list_decision(_trial_decisions())["decision"])
        for phrase in _DECISION_SAYS:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, decision)

    def test_the_2026_10_03_decision_is_recorded_in_the_users_words(self) -> None:
        entries = [entry for entry in _trial_decisions() if str(entry.get("at", "")).startswith("2026-10-03")]
        self.assertEqual(1, len(entries))
        decision = str(entries[0]["decision"])
        for phrase in _DECISION_2026_10_03_SAYS:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, decision)
        self.assertFalse(any(f"({name})" in decision for name in _GATE_LISTS), "it moves no check between the lists")

    def test_a_unit_flagged_by_its_studys_text_alone_is_not_stated_as_excluded(self) -> None:
        """Before 2026-10-03 the contract excluded ion-mobility data "whether the repository or the raw headers
        show it", and the plan read the repository's column, set from text many units share."""
        heading, _lead, _phrases = _DECIDED_CASES["ion mobility from unit-level evidence"]
        section = _section(self.contract, heading)
        earlier = section.replace("is not such evidence", "is evidence too")
        self.assertNotEqual(section, earlier)
        problems = _case_problems(self.contract.replace(section, earlier))
        self.assertTrue(any(item.startswith("ion mobility from unit-level evidence:") for item in problems), problems)
        self.assertNotIn("whether the repository or the raw headers show it", " ".join(section.split()))

    def test_every_description_of_the_discard_fallback_names_its_policy_field(self) -> None:
        found = {}
        for document in DOCUMENTS:
            text = (_ROOT / document).read_text(encoding="utf-8")
            if _fallback_without_its_field(text):
                found[document] = _fallback_without_its_field(text)
        self.assertEqual({}, found)
        # At 79a251c the fallback named no switch, so nothing told the runner whether the user allowed it.
        bare = self.contract.replace(f"`{_DISCARD_FALLBACK_FIELD}: true`", "an approval")
        self.assertEqual(2, len(_fallback_without_its_field(bare)))

    def test_the_lists_are_read_from_their_bullets_only(self) -> None:
        section = (
            "## Gate verdicts in a campaign\n\n"
            "- `blocks_run`: ELIG-1 and\n  ACQ-1. Such a unit fails.\n"
            "- `record_only`: CLS-1.\n\nSUM-1 is named here, in neither list.\n"
        )
        self.assertEqual({"ELIG-1", "ACQ-1"}, _gate_list(section, "blocks_run"))
        self.assertEqual({"CLS-1"}, _gate_list(section, "record_only"))
        decided = _decided_lists([
            {"decision": "Only ELIG-1 and ACQ-1 stop it (blocks_run). CLS-1 is recorded (record_only)."},
            {"decision": "Later: CNT-1 stops it too (blocks_run)."},
        ])
        self.assertEqual({"blocks_run": {"CNT-1"}, "record_only": {"CLS-1"}}, decided)
        text = ("a FAIL in a `blocks_run` check\n   (ELIG-1 and ACQ-1), and a `record_only` check (CLS-1); a "
                "`blocks_run` check (the user's decision) is no list.")
        self.assertEqual([{"ELIG-1", "ACQ-1"}], _parenthesised_lists(text, "blocks_run"))
        self.assertEqual([{"CLS-1"}], _parenthesised_lists(text, "record_only"))


class StopIsPerUnitTests(unittest.TestCase):
    """What the contract says a stop is, held to the runner the campaign runs (scripts/campaign)."""

    def setUp(self) -> None:
        self.contract = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        heading, lead, _phrases = _DECIDED_CASES["a stop is per unit"]
        self.passage = next(block for block in _blocks(_section(self.contract, heading)) if block.startswith(lead))

    def test_every_held_state_of_the_runner_is_named_in_the_gate_rule(self) -> None:
        sys.path.insert(0, str(_ROOT / "scripts"))
        try:
            from campaign import ledger
        finally:
            sys.path.remove(str(_ROOT / "scripts"))
        section = _section(self.contract, _GATE_RULE_SECTION)
        self.assertEqual(("gate_held", "contract_held"), ledger.HELD_STATES)
        for state in ledger.HELD_STATES:
            with self.subTest(state=state):
                self.assertIn(f"`{state}`", section)

    def test_the_runners_four_campaign_pauses_are_the_ones_named(self) -> None:
        """The pauses the contract names are the runner's own (policy.CAMPAIGN_PAUSES), each by its name."""
        sys.path.insert(0, str(_ROOT / "scripts"))
        try:
            from campaign import policy
        finally:
            sys.path.remove(str(_ROOT / "scripts"))
        self.assertEqual({"pin", "disk", "outage", "backend"}, set(policy.CAMPAIGN_PAUSES))
        for kind, name in policy.CAMPAIGN_PAUSES.items():
            with self.subTest(kind=kind):
                words = name.split(" ", 1)[1]  # "a pin change" is "A pin change, ..." in the passage
                self.assertIn(words, self.passage)

    def test_no_pause_waits_for_an_operator(self) -> None:
        """Before 2026-10-02 one unit's broken record paused the campaign until an operator resumed it."""
        for earlier in ("until an operator resumes", "only an operator lifts", "waits for an operator"):
            with self.subTest(earlier=earlier):
                self.assertNotIn(earlier, self.passage)
                put_back = self.contract.replace("Nothing one unit does pauses the campaign.",
                                                 f"A broken contract pauses the campaign {earlier}.")
                self.assertTrue(any(item.startswith("a stop is per unit:") for item in _case_problems(put_back)))


class TrialDecisionsTests(unittest.TestCase):
    """The trial manifest's decisions are appended as they are taken: each says when, and what."""

    def test_decisions_are_dated_and_in_order(self) -> None:
        decisions = json.loads(
            (_ROOT / "trials" / "2026-09-20-ten-unit-trial-manifest.json").read_text(encoding="utf-8")
        )["decisions"]
        for entry in decisions:
            with self.subTest(entry=entry.get("decision", "")[:60]):
                self.assertRegex(entry["at"], r"^\d{4}-\d{2}-\d{2}")
                self.assertTrue(str(entry["decision"]).strip())
        dates = [entry["at"][:10] for entry in decisions]
        self.assertEqual(sorted(dates), dates)


if __name__ == "__main__":
    unittest.main()
