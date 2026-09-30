"""The contract names files in this repository, and each name must resolve in this tree.

CLAUDE.md, README.md, AGENTS.md and the project skills tell a session which script to run, which
record to read and which template to return. A name that resolves to nothing sends that session
looking for a file that is not there, or lets it conclude that a step does not exist. Locations
outside this repository (the Interactive checkout, the analysis root) are not checked: they are not
this tree's to hold, and they differ from machine to machine.

A document may name a file that another change is bringing, as the campaign amendment names the
campaign runner before it lands. Such a name is listed in PENDING with what brings it, and the test
fails once the file exists, so that the entry is removed rather than left to excuse a later broken
name.
"""

from __future__ import annotations

import json
import re
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
# The documents also name this repository by its absolute location on the author's machine.
_ABSOLUTE_ROOT = re.compile(r"D:\\13_MSDIAL_Public_Reanalysis\\code\\([^\s`)'\"]+)", re.IGNORECASE)
_FENCE = re.compile(r"^```.*?^```[^\n]*$", re.MULTILINE | re.DOTALL)
_SPAN = re.compile(r"`([^`\n]+)`")

PENDING = {
    "scripts/campaign-runner.py": "plan item 21, the campaign runner (feat/campaign-runner)",
}
# Named on purpose and kept out of the repository by .gitignore: audit outputs under feedback/ are
# local by default, and only the review template is version controlled (README).
LOCAL_ONLY = frozenset({"feedback/codex-pre-audit-2026-09-02.md"})


def _named_paths(text: str) -> set[str]:
    """Repository-relative paths a document names, in code spans and as absolute locations."""
    names = {match.group(1) for match in _ABSOLUTE_ROOT.finditer(text)}
    # A fenced block is removed before spans are paired: its three backticks would otherwise shift
    # every pairing after it, and the spans read would be the prose between them.
    for match in _SPAN.finditer(_FENCE.sub("", text)):
        words = match.group(1).split()
        if not words:
            continue
        token = words[0].replace("\\", "/")
        if token.split("/", 1)[0] in _REPOSITORY_FOLDERS:
            names.add(token)
    cleaned = set()
    for name in names:
        name = name.replace("\\", "/").rstrip(".,;:")
        if not any(mark in name for mark in "<>*"):
            cleaned.add(name)
    return cleaned


class ContractReferencesTests(unittest.TestCase):
    def test_every_repository_path_the_contract_names_exists(self) -> None:
        missing = {}
        for document in DOCUMENTS:
            text = (_ROOT / document).read_text(encoding="utf-8")
            for name in _named_paths(text):
                if name not in PENDING and name not in LOCAL_ONLY and not (_ROOT / name).exists():
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
