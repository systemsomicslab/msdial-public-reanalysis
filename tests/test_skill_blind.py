"""Blind evaluation (decision of 2026-11-18, D3): what the triage agent reads, and the code that prepares and expands its
work, never carries an answer of the eval set.

The eval accessions (tests/triage_eval.ACCESSIONS), their analysis ids (the fixtures' catalog.json) and the top-level
folder names of their archives (archive-members.tsv) may appear only in test data (tests/fixtures/, triage_eval.py) and
in the docs, never in the skill, the v2 prompt templates, the spec schema, the digest script or the expander. The rules
stay general; the answers live in tests/fixtures/triage-eval/ and tests/fixtures/triage-eval-specs/. Also checked: one
skill version, 1.3.0, wherever the code and the tests name the current one.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
import decision_records as records  # noqa: E402
import triage_eval as te  # noqa: E402

SKILL_VERSION = "1.3.0"
SEEN_BY_THE_AGENT = [
    ".claude/skills/msdial-unit-triage/SKILL.md",
    "prompts/unit-triage-task.v2.md",
    "prompts/unit-triage-revision.v2.md",
    "schemas/msdial-unit-triage-spec.v1.json",
    "scripts/triage-digest.py",
    "scripts/expand-triage-spec.py",
    "scripts/triage-spec-server.py",
]


def eval_terms() -> set[str]:
    terms = set(te.ACCESSIONS)
    for accession in te.ACCESSIONS:
        catalog = json.loads((te.fixture_dir(accession) / "catalog.json").read_text(encoding="utf-8"))
        for unit in catalog["units"]:
            terms.add(unit["unit_id"])
            if unit.get("source_subrecord_id"):
                terms.add(unit["source_subrecord_id"])
        with (te.fixture_dir(accession) / "archive-members.tsv").open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                path = row["path"].replace("\\", "/").strip("/")
                if "/" in path:
                    top = path.split("/", 1)[0]
                    if top not in ("__MACOSX", "data"):  # a generic word, not an answer
                        terms.add(top)
    return terms


class BlindTests(unittest.TestCase):
    def test_no_eval_answer_in_what_the_agent_reads_or_the_code_that_prepares_it(self) -> None:
        terms = eval_terms()
        self.assertGreater(len(terms), 30)
        for relative in SEEN_BY_THE_AGENT:
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(relative):
                found = sorted(t for t in terms if re.search(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", text))
                self.assertEqual(found, [], f"{relative} names eval-set identifiers")

    def test_the_v2_prompts_use_synthetic_ids(self) -> None:
        for relative in ("prompts/unit-triage-task.v2.md", "prompts/unit-triage-revision.v2.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("ST000001", text)
            for found in re.findall(r"\b(?:ST|AN)[0-9]{6}\b", text):
                self.assertRegex(found, r"^(ST|AN)00000[0-9]$", relative)


class VersionTests(unittest.TestCase):
    def test_one_skill_version(self) -> None:
        skill = (ROOT / ".claude/skills/msdial-unit-triage/SKILL.md").read_text(encoding="utf-8")
        self.assertIn(f"Skill version: **{SKILL_VERSION}**", skill)
        # Hand-written v2 fixtures name the skill that wrote payloads by hand, never the current one (T-SPEC refuses a
        # payload without a derivation from skill 1.2.0 on).
        self.assertEqual(te.to_v2(te.build_good("ST002747"))["AN004454"]["decided_by"]["skill_version"],
                         records.HAND_WRITTEN_SKILL_VERSION)
        self.assertLess(tuple(int(p) for p in records.HAND_WRITTEN_SKILL_VERSION.split(".")),
                        tuple(int(p) for p in SKILL_VERSION.split(".")))
        for relative in ("tests/test_run_triage_agent.py", "tests/test_validate_triage_v2.py"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(f'"{SKILL_VERSION}"', text, relative)
        docs = (ROOT / "docs/llm-assisted-curation.md").read_text(encoding="utf-8")
        self.assertIn(f"skill {SKILL_VERSION}", docs)
        self.assertIn(f'"version": "{SKILL_VERSION}"', docs)


if __name__ == "__main__":
    unittest.main()
