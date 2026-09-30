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
rule must place every before-production check the gate runs, and the two lists the user gave must
still hold the checks the trial manifest records them giving.
"""

from __future__ import annotations

import importlib.util
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

PENDING = {
    "scripts/campaign-runner.py": "plan item 21, the campaign runner (feat/campaign-runner)",
}
# Named on purpose and kept out of the repository by .gitignore: audit outputs under feedback/ are
# local by default, and only the review template is version controlled (README).
LOCAL_ONLY = frozenset({"feedback/codex-pre-audit-2026-09-02.md"})

# A gate check id: letters, a hyphen and one digit (SUM-1, CONV-1). Tokens of that shape that are
# not checks are listed here.
_CHECK_ID = re.compile(r"\b[A-Z]{2,5}-[1-9]\b")
_NOT_CHECKS = frozenset({"UTF-8"})
_GATE_RULE_SECTION = "## Gate verdicts in a campaign"
_GATE_LISTS = ("blocks_run", "record_only")


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


def _gate_checks_by_stage() -> dict[str, list[str]]:
    """The check ids the gate runs at each stage, read from the gate itself on an empty workspace.

    Every check reports on a workspace with no artifacts (not_evaluable, never skipped), so this is
    the gate's own list, not one written down beside it.
    """
    path = _ROOT / "scripts" / "verify-run-invariants.py"
    spec = importlib.util.spec_from_file_location("verify_run_invariants_contract", path)
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    sys.modules["verify_run_invariants_contract"] = verifier
    spec.loader.exec_module(verifier)
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

    def test_the_campaign_gate_rule_places_every_before_production_check(self) -> None:
        rule = _section(self.contract, _GATE_RULE_SECTION)
        self.assertEqual(set(self.stages["before-production"]), _check_ids(rule))

    def test_the_two_lists_hold_the_checks_the_user_gave_them(self) -> None:
        rule = _section(self.contract, _GATE_RULE_SECTION)
        lists = {name: _gate_list(rule, name) for name in _GATE_LISTS}
        self.assertFalse(lists["blocks_run"] & lists["record_only"], "a check is in both lists")
        decisions = json.loads(
            (_ROOT / "trials" / "2026-09-20-ten-unit-trial-manifest.json").read_text(encoding="utf-8")
        )["decisions"]
        decided = _decided_lists(decisions)
        self.assertEqual(set(_GATE_LISTS), set(decided), "the trial manifest records no decision for a list")
        for name in _GATE_LISTS:
            with self.subTest(list=name):
                self.assertTrue(decided[name])
                self.assertLessEqual(decided[name], lists[name])

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
