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
before-production FAILs stop a unit's run (user decision, 2026-10-01). A check named there that the
gate does not run is a rule nobody applies, and a before-production check the rule leaves out is one
whose FAIL nobody has placed. So every named check must be one the gate runs, the campaign's gate
rule must place every before-production check the gate runs, the two lists the user gave must hold
exactly the checks the trial manifest records them giving, and every other before-production check
must be named as in neither list. A list may not gain a check in either direction: one added to
record_only is a FAIL that no longer stops a run, and one added to blocks_run is a unit failed and
its raw data deleted, and neither is the contract's to decide.

The user's rule says what a FAIL in the two lists does and leaves cases open: a FAIL in a check in
neither list, a check left not_evaluable, a gate that produced no report. The contract says what
applies to each until the user decides, and a reader must be able to tell that reading from the
user's own rule. So every passage that says what an open case does to a unit's run is marked as
awaiting the user's decision, and every passage that describes the confirmed=true discard fallback
names the policy field that carries the user's approval of it.
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
_GATE_LISTS = ("blocks_run", "record_only")
# The paragraph of the gate rule that names the before-production checks the user placed in neither
# list.
_NEITHER_LIST = "**In neither list, awaiting the user's decision.**"
# What marks a passage as the contract's reading of a case the user has not settled. The discard
# fallback is marked "awaiting the user's approval" instead, and named by its policy field.
_AWAITING = "awaiting the user's decision"
# A passage that says what a case does to a unit's run.
_RUN_OUTCOME = re.compile(r"\bthe unit runs?\b|\bstops? the run\b|\bholds? the run\b", re.IGNORECASE)
# Cases that are not a FAIL, so the user's gate rule of 2026-10-01 does not reach them: a check left
# not_evaluable, and a gate that produced no report. Each stays open until a trial decision names it.
_OPEN_VERDICTS = ("not_evaluable", "no report")
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


def _decided_lists(decisions: list[dict]) -> dict[str, set[str]]:
    """For each list, the checks named by the latest decision sentence that puts checks in it."""
    found: dict[str, set[str]] = {}
    for entry in decisions:
        for sentence in re.split(r"(?<=[.])\s+", str(entry.get("decision") or "")):
            for name in _GATE_LISTS:
                if f"({name})" in sentence:
                    found[name] = _check_ids(sentence)
    return found


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


def _moved_into(contract: str, name: str, checks: set[str]) -> str:
    """The contract with `checks` added at the head of the `name` bullet of the gate rule."""
    marker = f"- `{name}`: "
    start = contract.index(marker) + len(marker)
    return contract[:start] + ", ".join(sorted(checks)) + ", " + contract[start:]


def _without_neither_list(contract: str) -> str:
    paragraph = r"\n" + re.escape(_NEITHER_LIST) + r".*?\n[ \t]*\n"
    return re.sub(paragraph, "\n", contract, count=1, flags=re.DOTALL)


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


def _open_verdicts(decisions: list[dict]) -> tuple[str, ...]:
    """The verdicts that are not a FAIL and that no decision names yet."""
    named = " ".join(str(entry.get("decision") or "") for entry in decisions)
    return tuple(phrase for phrase in _OPEN_VERDICTS if phrase not in named)


def _unmarked_open_cases(text: str, unplaced: set[str], open_verdicts: tuple[str, ...]) -> list[str]:
    """Passages that say what a case the user has not settled does, with no mark that it awaits the user.

    A passage speaks to such a case when it names a FAIL of a before-production check the user placed
    in neither list, or says what a still-open verdict (a check left not_evaluable, a gate that
    produced no report) does to a unit's run.
    """
    found = []
    for block in _blocks(text):
        unplaced_fail = bool(_check_ids(block) & unplaced) and "FAIL" in block
        open_verdict = any(phrase in block for phrase in open_verdicts) and _RUN_OUTCOME.search(block)
        if (unplaced_fail or open_verdict) and _AWAITING not in block.casefold():
            found.append(block[:160])
    return found


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
        for name in _GATE_LISTS:
            with self.subTest(list=name):
                self.assertEqual(decided[name], lists[name])

    def test_the_checks_the_user_placed_in_neither_list_are_named_as_such(self) -> None:
        before_production = set(self.stages["before-production"])
        self.assertEqual([], _gate_rule_disagreements(self.contract, before_production, self._decided()))

    def test_a_list_widened_with_the_unplaced_checks_is_refused(self) -> None:
        before_production = set(self.stages["before-production"])
        decided = self._decided()
        unplaced = before_production - decided["blocks_run"] - decided["record_only"]
        self.assertTrue(unplaced, "every before-production check is placed; this test has nothing to move")
        # Moved into record_only, their FAILs would stop no run; moved into blocks_run, as the draft once
        # read them, each FAIL would fail the unit and delete its raw data. The user decided neither.
        for name in _GATE_LISTS:
            with self.subTest(list=name):
                widened = _without_neither_list(_moved_into(self.contract, name, unplaced))
                self.assertNotIn(_NEITHER_LIST, widened)
                self.assertEqual(before_production, _check_ids(_section(widened, _GATE_RULE_SECTION)))
                problems = _gate_rule_disagreements(widened, before_production, decided)
                self.assertTrue(any(item.startswith(f"{name} holds") for item in problems), problems)
                self.assertTrue(any(_NEITHER_LIST in item for item in problems), problems)
                kept = _moved_into(self.contract, name, unplaced)
                self.assertTrue(_gate_rule_disagreements(kept, before_production, decided))

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
        self.assertTrue(set(report["strict_failures"]) & decided["blocks_run"], report["strict_failures"])

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
            "the paragraph deleted": (_without_neither_list(section), gate, decided),
            "an unplaced check in a list": (_moved_into(section, "record_only", {"SUM-1"}), gate, decided),
            "a placed check called unplaced": (section.replace("SUM-1, and", "SUM-1 and CLS-1, and"),
                                               gate, decided),
            "a later decision the rule missed": (section, gate,
                                                 {**decided, "record_only": {"CLS-1", "SUM-1"}}),
        }
        for case, (text, checks, lists) in cases.items():
            with self.subTest(case=case):
                self.assertTrue(_gate_rule_disagreements(text, checks, lists))

    def _unplaced(self) -> set[str]:
        decided = self._decided()
        return set(self.stages["before-production"]) - decided["blocks_run"] - decided["record_only"]

    def test_every_case_the_user_left_open_is_marked_as_awaiting_the_user(self) -> None:
        unplaced = self._unplaced()
        open_verdicts = _open_verdicts(_trial_decisions())
        self.assertTrue(unplaced or open_verdicts, "nothing is open; this test has nothing to hold")
        found = {}
        for document in DOCUMENTS:
            text = (_ROOT / document).read_text(encoding="utf-8")
            if _unmarked_open_cases(text, unplaced, open_verdicts):
                found[document] = _unmarked_open_cases(text, unplaced, open_verdicts)
        self.assertEqual({}, found)

    def test_the_open_cases_written_as_settled_rule_are_refused(self) -> None:
        # At 79a251c the contract stated its reading of the open cases as rule: "**In neither list.**",
        # "Nothing else stops the run." and the mzXML polarity sentence carried no mark.
        unplaced = self._unplaced()
        open_verdicts = _open_verdicts(_trial_decisions())
        unmarked = re.sub(r",? awaiting the user's decision", "", self.contract, flags=re.IGNORECASE)
        found = _unmarked_open_cases(unmarked, unplaced, open_verdicts)
        self.assertEqual(4, len(found), found)
        for lead in ("**In neither list.**", "**Not evaluable.**", "**No report.**",
                     "- **mzXML without a polarity.**"):
            with self.subTest(lead=lead):
                self.assertTrue(any(block.startswith(lead) for block in found), found)

    def test_a_case_is_held_open_however_a_passage_settles_it(self) -> None:
        unplaced, open_verdicts = {"CONV-1"}, ("not_evaluable", "no report")
        # Each case as a passage that settles it, then the same passage marked. A mark in another list
        # item does not count.
        cases = {
            "an unplaced FAIL": (
                "A FAIL in CONV-1 is recorded, and the unit runs.",
                "Awaiting the user's decision, a FAIL in CONV-1 is recorded, and the unit runs."),
            "an unplaced FAIL in a list item": (
                "- First, awaiting the user's decision.\n- CONV-1 FAILs the conversion, and it runs.\n",
                "- First.\n- CONV-1 FAILs the conversion (awaiting the user's decision), and it runs.\n"),
            "not evaluable": (
                "A check left `not_evaluable` stops\nthe run.",
                "**Open, awaiting the user's decision.** A check left `not_evaluable` stops\nthe run."),
            "no report": (
                "A gate that produced no report is recorded, and the unit runs.",
                "A gate that produced no report, awaiting the user's decision, stops the run."),
            "no report, put otherwise": (
                "A gate with no report lets the unit run.",
                "Awaiting the user's decision, a gate with no report lets the unit run."),
        }
        for case, (settled, marked) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(1, len(_unmarked_open_cases(settled, unplaced, open_verdicts)))
                self.assertEqual([], _unmarked_open_cases(marked, unplaced, open_verdicts))
        for text in (
            "A FAIL in CLS-1 is recorded, and the unit runs.",
            "CONV-1 holds each converted input to its record.",
            "Treat `not_evaluable` as a reason to stop, not as consent.",
            "```text\nA FAIL in CONV-1 stops the run.\n```\n",
        ):
            with self.subTest(text=text):
                self.assertEqual([], _unmarked_open_cases(text, unplaced, open_verdicts))
        decided = [{"decision": "A check left not_evaluable stops the run (blocks_run)."}]
        self.assertEqual(("no report",), _open_verdicts(decided))

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
