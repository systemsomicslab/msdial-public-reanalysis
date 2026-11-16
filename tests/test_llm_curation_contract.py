"""docs/llm-assisted-curation.md, the transparency and replay contract for LLM-assisted curation.

The contract is what the build PRs implement and what the Methods are written from, so its normative parts are
tested here: the reference implementation of the canonical selection must reproduce the worked example's sha256,
and the sections the review of 2026-11-16 found inconsistent with the builds (the selection, the context the model
saw, the decision ledger, the replay levels, header reconciliation, revisions, redaction, sampling and the Methods)
must say what the builds do. Synthetic accessions only.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "llm-assisted-curation.md"

# The kinds the runner exports to decision-ledger.jsonl (scripts/campaign/curated.py DECISION_KINDS).
LEDGER_KINDS = (
    "triage_recorded", "triage_validated", "manifest_approved", "unit_reopened", "triage_checked",
    "curated_selection_applied", "header_reconciliation", "cross_unit_verdict", "terminal_refusal", "person_answer",
)


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """The body of the section whose heading line starts with ``heading`` (up to the next heading of its level)."""
    level = len(heading) - len(heading.lstrip("#"))
    lines: list[str] | None = None
    fenced = False
    for line in _text().splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"^#{1,6} ", line):
            if lines is not None and len(line) - len(line.lstrip("#")) <= level:
                break
            if lines is None and line.startswith(heading):
                lines = []
                continue
        if lines is not None:
            lines.append(line)
    if lines is None:
        raise AssertionError(f"section {heading!r} not found")
    return "\n".join(lines)


def _fence(info: str) -> str:
    """The content of the fenced block whose info string is exactly ``info``."""
    match = re.search(rf"^```{re.escape(info)}\n(.*?)^```", _text(), flags=re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f"no fenced block ```{info}")
    return match.group(1)


def _reference() -> dict:
    """The section 3.1 reference implementation, executed from the document itself."""
    match = re.search(
        r"^```python\n(# msdial-selection-canonical\.v1 reference implementation\n.*?)^```",
        _text(), flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError("the document carries no msdial-selection-canonical.v1 reference implementation")
    namespace: dict = {}
    exec(compile(match.group(1), str(DOC), "exec"), namespace)  # noqa: S102 - the document's own code
    return namespace


class SelectionCanonicalForm(unittest.TestCase):
    """Finding 1: one normative selection, so the plan, the runner, Interactive and the replay agree."""

    def test_reference_reproduces_the_worked_example(self):
        ref = _reference()
        example = json.loads(_fence("json selection-example"))
        expected = _fence("text selection-example-sha256").strip()
        self.assertRegex(expected, r"^[0-9a-f]{64}$")
        self.assertEqual(ref["selection_sha256"](example["triage"], example["listing"]), expected)
        entries = ref["selection_entries"](example["triage"], example["listing"])
        self.assertEqual([entry["member_path"] for entry in entries],
                         ["study/POS/QC01.mzML", "study/POS/S01.mzML", "study/POS/S02.mzML"])
        self.assertEqual([entry["crc32"] for entry in entries], [None, "ABCDEF01", "00BCDEF2"])

    def test_the_no_listing_value_the_document_states_is_right(self):
        ref = _reference()
        example = json.loads(_fence("json selection-example"))
        value = ref["selection_sha256"](example["triage"], {})
        self.assertIn(f"no listing gives `{value}`", _section("### 3.1"))

    def test_added_members_are_in_the_selection(self):
        ref = _reference()
        triage = {"unit_id": "AN000001", "rows": [{"row_index": 1, "member_path": "a/S01.mzML"}],
                  "added_members": [{"member_path": "a/QC01.mzML"}]}
        self.assertEqual(len(ref["selection_entries"](triage, {})), 2)

    def test_a_child_keeps_the_bare_unit_id_and_its_own_members(self):
        ref = _reference()
        triage = {"unit_id": "AN000001",
                  "rows": [{"row_index": 1, "member_path": "pos/S01.mzML"}, {"row_index": 2, "member_path": "neg/S01.mzML"}],
                  "children": [{"child_key": "pos", "member_paths": ["POS/s01.mzml"]}]}
        entries = ref["selection_entries"](triage, {}, triage["children"][0])
        self.assertEqual([(entry["unit_id"], entry["child_key"], entry["member_path"]) for entry in entries],
                         [("AN000001", "pos", "pos/S01.mzML")])

    def test_crc32_and_size_come_from_the_listing_never_the_triage(self):
        ref = _reference()
        bare = {"unit_id": "AN000001", "rows": [{"row_index": 1, "member_path": "a/S01.mzML"}]}
        claimed = {"unit_id": "AN000001", "rows": [{"row_index": 1, "member_path": "a/S01.mzML",
                                                     "crc32": "DEADBEEF", "size": 99}]}
        self.assertEqual(ref["selection_sha256"](bare, {}), ref["selection_sha256"](claimed, {}))
        listing = {"a/s01.mzml": {"crc32": "1", "size": "7"}}
        entry = ref["selection_entries"](claimed, listing)[0]
        self.assertEqual((entry["crc32"], entry["size"]), ("00000001", 7))

    def test_the_hash_is_order_independent_and_canonical(self):
        ref = _reference()
        rows = [{"row_index": index, "member_path": f"a/S{index:02d}.mzML"} for index in range(1, 5)]
        forward = {"unit_id": "AN000001", "rows": rows}
        backward = {"unit_id": "AN000001", "rows": list(reversed(rows))}
        self.assertEqual(ref["selection_sha256"](forward, {}), ref["selection_sha256"](backward, {}))
        payload = ref["canonical_json"](ref["selection_entries"](forward, {}))
        self.assertEqual(ref["selection_sha256"](forward, {}), hashlib.sha256(payload).hexdigest())
        self.assertNotIn(b" ", payload)

    def test_the_runner_compares_the_value_interactive_recorded(self):
        self.assertIn("holds the unit when\nthey differ", _section("### 2.5"))


class ContextTheModelSaw(unittest.TestCase):
    """Finding 2: the harness context is controlled and recorded, and the Methods no longer overclaim."""

    def test_the_overclaim_is_gone(self):
        self.assertNotIn("Everything the LLM saw is released", _text())

    def test_the_envelope_records_a_clean_configuration(self):
        envelope = json.loads(_fence("json"))
        context = envelope["agent"]["context"]
        self.assertEqual(context["configuration"], "clean")
        self.assertEqual(context["context_files"], [])
        self.assertEqual(context["memory"], "disabled")
        self.assertIn("not `clean`", _section("### 2.3"))

    def test_the_rerun_uses_the_same_configuration(self):
        self.assertIn("same clean configuration", _section("### 4.1"))


class HashesThatCanBeChecked(unittest.TestCase):
    """Finding 6: every hash the validator recomputes covers stored bytes; tool_calls are not self-reported."""

    def test_rendered_prompt_tool_results_and_transcript_bytes_are_stored(self):
        envelope = json.loads(_fence("json"))
        self.assertIn("rendered_path", envelope["prompt"])
        self.assertTrue(all("result_path" in call for call in envelope["tool_calls"]))
        self.assertIn("released redacted bytes", envelope["transcript"]["sha256"])
        self.assertIn("unredacted_sha256", envelope["transcript"])

    def test_the_wrapper_writes_the_envelope_and_the_validator_cross_checks_the_transcript(self):
        section = _section("### 2.3")
        self.assertIn("written by the wrapper", section)
        self.assertIn("differs from the tool calls the released transcript shows", section)
        self.assertIn("What `validate-triage.py` recomputes", section)


class DecisionLedger(unittest.TestCase):
    """Finding 3: the campaign's decision-ledger.jsonl is named, mapped, checked and released."""

    def test_every_runner_ledger_kind_is_mapped(self):
        section = _section("### 2.4")
        self.assertIn("decision-ledger.jsonl", section)
        for kind in LEDGER_KINDS:
            self.assertIn(f"| `{kind}` |", section, kind)

    def test_the_verifier_walks_the_ledger(self):
        self.assertIn("walks every review log and the ledger", _section("### 2.4"))

    def test_the_ledger_is_released(self):
        self.assertIn("the decision ledger", _section("### 5.3"))


class ReplayLevels(unittest.TestCase):
    """Findings 4 and 5: the inputs level compares what the recorded projection names; outputs are canonical."""

    def test_the_inputs_level_follows_the_projection_schema(self):
        section = _section("### 3.2")
        self.assertIn("msdial-replay-projection.v1", section)
        self.assertIn("does not\n    cover libraries, the Console identity or the parameter file", section)
        self.assertIn("msdial-parameter-file-canonical.v1", section)
        self.assertNotIn("`parameter_file_sha256`", _text())

    def test_the_outputs_level_is_compared_after_finalisation(self):
        section = _section("### 3.3")
        self.assertIn("ms_run[n]-location", section)
        self.assertIn("database[n]-uri", section)
        self.assertIn("never released", section)


class RevisionsAndAnswers(unittest.TestCase):
    """Findings 7 and 8: answers are read from the review log by code; a validator error redo has a cause."""

    def test_v2_reads_person_answers_from_the_review_log_by_code(self):
        section = _section("### 2.2")
        self.assertIn("reads each accession's `review-log.jsonl`", section)
        self.assertIn("matching `question_code`", section)
        self.assertIn("without a `person_answer` of the matching `question_code`", _section("### 2.4"))

    def test_a_validator_verdict_is_an_admissible_revision_cause(self):
        row = next(line for line in _section("### 2.4").splitlines() if line.startswith("| `revision` |"))
        self.assertIn("`validator_verdict`", row)
        self.assertIn("`<unit_id>.r<N>.json`", _section("## 6."))


class HeaderReconciliation(unittest.TestCase):
    """Finding 9: the scope, shape and code are what Interactive builds."""

    def test_the_code_and_scope_match_interactive(self):
        text = _text()
        self.assertNotIn("folder_header_disagreement", text)
        section = _section("## 6.")
        self.assertIn("triage_header_disagreement", section)
        self.assertIn("MS levels, DIA window signature and run end time are not compared", section)
        self.assertIn("ion_mode: {triage, header, result}", section)


class Release(unittest.TestCase):
    """Finding 10: manifests are released as a projection; the redaction rule covers UNC paths and context."""

    def test_manifests_are_released_as_a_projection(self):
        section = _section("### 5.3")
        self.assertIn("Release projection", section)
        self.assertIn("release-index.json", section)

    def test_the_redaction_rule_covers_unc_paths_and_context_blocks(self):
        section = _section("### 5.3")
        self.assertIn("UNC path", section)
        self.assertIn("context\nfile or memory", section)


class SamplingAndMethods(unittest.TestCase):
    """Findings 11 and 12: single-unit accessions are sampled, undefined kappa is handled, redos are disclosed."""

    def test_single_unit_accessions_are_a_stratum(self):
        self.assertIn("unit count (1, 2, 3 to 4, 5 or more)", _section("### 4.2"))

    def test_undefined_kappa_is_handled(self):
        self.assertIn("kappa is undefined", _section("### 4.2"))
        self.assertIn("{n_kappa_undefined}", _section("### 5.1"))

    def test_regenerations_are_disclosed(self):
        section = _section("### 5.1")
        self.assertIn("{n_redo}", section)
        self.assertIn("{n_units_redo}", section)


if __name__ == "__main__":
    unittest.main()
