"""docs/llm-assisted-curation.md, the transparency and replay contract for LLM-assisted curation.

The contract is what the build PRs implement and what the Methods are written from, so its normative parts are
tested here: the reference implementation of the canonical selection must reproduce the worked example's sha256,
and the sections the reviews of 2026-11-16 found inconsistent with the builds (the selection, the context the model
saw, the decision ledger, the replay levels, header reconciliation, revisions, redaction, sampling and the Methods)
must say what the builds do. The WS0 settlement of 2026-11-16 (Interactive #102 d83ed5b, runner #60 64f7983) adds
the run-time layout, the download arguments, the applied selection's location, the cross-unit vocabulary and rules,
the release bundle the replay reads and what needs a Console run. The WS0 decisions of 2026-11-17 (Interactive #102
39f42f2, runner #60 f25e5b8) add the campaign ratification left out of the handoff comparison (A), a unit refused
after it ran (B), the triage inputs released as projections (C), the stated role over the name filter (D), and the
lows (a single member, the plan-time workspace name, the class drift stop, the reference's child key, the header
disagreement answers). WS0 decision F of 2026-11-17 (Interactive #102 0c2921a, runner #60 87c4fe3) supersedes A's
scope: the triage runs before the Class and binds the evidence it read, and the Class binds through its own
ratification and the class digest; the handoff comparison leaves out what the Class decides (HANDOFF_CLASS_KEYS), and
the triage validator refuses a triage that cites the Class (T-CLASS). Synthetic accessions only.

Where Interactive is importable (MSDIAL_INTERACTIVE_ROOT names a checkout of 0.5.69 or later), or this checkout
holds the runner's scripts/campaign/curated.py, the reference implementation is also compared with theirs.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "llm-assisted-curation.md"

# The kinds the runner exports to decision-ledger.jsonl (scripts/campaign/curated.py DECISION_KINDS).
LEDGER_KINDS = (
    "triage_recorded", "triage_validated", "manifest_approved", "unit_reopened", "triage_checked",
    "curated_selection_applied", "header_reconciliation", "cross_unit_verdict", "terminal_refusal", "person_answer",
)
# The review log's kinds and fields (scripts/campaign/curated.py REVIEW_LOG_KINDS at runner #60 64f7983).
REVIEW_LOG_KINDS = {
    "validator_verdict": ("tool", "code_commit", "verdict", "findings_sha256", "findings"),
    "person_answer": ("by", "question_code", "answer", "channel"),
    "revision": ("supersedes", "new_decision_id", "cause"),
    "digest_approval": ("manifest_digest", "approval_id", "by"),
}
# msdial_download_repository_raw's curated arguments (Interactive #102 d83ed5b) the runner passes.
DOWNLOAD_ARGUMENTS = (
    "analysis_unit_handoff_path", "curated_selection_path", "decision_record_path", "original_handoff_sha256",
    "original_handoff_path", "curated_selection_validator", "curated_selection_child_key", "workspace_unit_key",
    "campaign_authorization_path",
)
# The cross-unit rules and their refusal codes (Interactive curated_selection, rule cross_unit_attribution_2026_11_16b).
CROSS_UNIT_RULES = {
    "P": "cross_unit_polarity_contradiction",
    "C": "cross_unit_inputs_indistinguishable_chromatography",
    "S": "cross_unit_inputs_contained",
    "D": "cross_unit_duplicate_inputs",
}
# The keys of Interactive 0.5.69's msdial-replay-projection.v1 (curated_selection.replay_projection).
PROJECTION_V1_KEYS = (
    "schema", "repository", "accession", "analysis_unit_id", "catalog_handoff_sha256", "curated_selection", "ion_mode",
    "acquisition_mode", "separation", "inputs", "analysis_csv_canonical_sha256", "peak_height",
    "msdial_interactive_version",
)
# Interactive's workspace_unit_key pattern (repository_reanalysis._WORKSPACE_UNIT_KEY).
WORKSPACE_UNIT_KEY = "[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}"
# What the Class decides, which the handoff comparison leaves out (WS0 decision F; Interactive #102 0c2921a,
# curated_selection.HANDOFF_CLASS_KEYS; the runner's copy in scripts/campaign/curated.py at #60 87c4fe3): the whole
# class_proposal, the blocking_reasons codes the Catalog's handoff builder adds from the Class alone, and the readiness
# keys, left out only where the remaining blocking_reasons are equal.
HANDOFF_CLASS_KEYS = {
    "keys": ("class_proposal",),
    "blocking_reason_codes": ("class_proposal:missing", "class_proposal:not_accepted", "class_proposal:other_unit"),
    "readiness_keys": ("next_action", "ready_for_download_planning"),
}
# A unit that ran and a later cross-unit check refused (runner #60 f25e5b8, ledger schema 5).
INVALIDATED_AFTER_DONE = "invalidated_after_done"
INVALIDATION_QUESTION = "cross_unit_refused_after_done"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Text with every run of whitespace (line breaks and list indentation included) as one space."""
    return " ".join(text.split())


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


def _says(section: str, *phrases: str) -> None:
    """Every phrase is in the section, whatever the line breaks."""
    body = _flat(section)
    for phrase in phrases:
        if _flat(phrase) not in body:
            raise AssertionError(f"the section does not say {phrase!r}")


def _fence(info: str) -> str:
    """The content of the fenced block whose info string is exactly ``info``."""
    match = re.search(rf"^```{re.escape(info)}\n(.*?)^```", _text(), flags=re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f"no fenced block ```{info}")
    return match.group(1)


def _table_row(section: str, first_cell: str) -> str:
    return next(line for line in section.splitlines() if line.startswith(f"| `{first_cell}` |"))


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


def _example() -> dict:
    return json.loads(_fence("json selection-example"))


def _listing_tsv(listing: dict) -> bytes:
    lines = ["archive\tpath\tsize\tcrc32\tcomment"]
    for member, item in listing.items():
        lines.append(f"{item['archive']}\t{member}\t{item['size']}\t{item['crc32']}\tignored")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cases() -> list[tuple[dict, dict, dict | None]]:
    """Synthetic (triage, listing, child) cases: the worked example, children, added members, ties, no listing."""
    example = _example()
    split = {"unit_id": "AN000002",
             "rows": [{"row_index": 1, "member_path": "pos/S01.mzML"}, {"row_index": 2, "member_path": "neg\\S01.mzML"},
                      {"row_index": 3, "member_path": "/pos/S02.mzML/", "archive": "B.zip"}],
             "added_members": [{"member_path": "pos/QC.mzML"}, {"member_path": "pos/QC.mzML", "archive": "C.zip"}],
             "children": [{"child_key": "pos", "member_paths": ["POS/s01.mzml", "pos/S02.mzML", "pos/qc.mzml"]},
                          {"child_key": "neg", "member_paths": ["neg/S01.mzML"]}]}
    listing = {"pos/s01.mzml": {"archive": "A.zip", "crc32": "1f", "size": 10},
               "pos/qc.mzml": {"archive": "A.zip", "crc32": "FFFFFFFF", "size": "3"}}
    cases: list[tuple[dict, dict, dict | None]] = [(example["triage"], example["listing"], None),
                                                   (example["triage"], {}, None)]
    for bound in (listing, {}):
        cases.append((split, bound, None))
        cases += [(split, bound, child) for child in split["children"]]
    return cases


class SelectionCanonicalForm(unittest.TestCase):
    """Finding 1 and WS0 decision 1: one normative selection, so the plan, the runner, Interactive and the replay
    agree."""

    def test_reference_reproduces_the_worked_example(self):
        ref = _reference()
        example = _example()
        expected = _fence("text selection-example-sha256").strip()
        self.assertRegex(expected, r"^[0-9a-f]{64}$")
        self.assertEqual(ref["selection_sha256"](example["triage"], example["listing"]), expected)
        entries = ref["selection_canonical"](example["triage"], example["listing"])
        self.assertEqual([entry["member_path"] for entry in entries],
                         ["study/POS/QC01.mzML", "study/POS/S01.mzML", "study/POS/S02.mzML"])
        self.assertEqual([entry["crc32"] for entry in entries], [None, "ABCDEF01", "00BCDEF2"])

    def test_the_reference_carries_interactives_names(self):
        ref = _reference()
        self.assertEqual(ref["SELECTION_CANONICAL_FORM"], "msdial-selection-canonical.v1")
        self.assertIs(ref["selection_entries"], ref["selection_canonical"])
        _says(_section("### 3.1"), "`msdial_app.curated_selection`", "`selection_canonical` (alias `selection_entries`)",
              "`parse_archive_listing`", "`scripts/campaign/curated.py`")

    def test_the_no_listing_value_the_document_states_is_right(self):
        ref = _reference()
        example = _example()
        value = ref["selection_sha256"](example["triage"], {})
        self.assertIn(f"no listing gives `{value}`", _section("### 3.1"))
        self.assertEqual(ref["selection_sha256"](example["triage"]), value)
        self.assertEqual(ref["selection_sha256"](example["triage"], None), value)

    def test_the_listing_parsed_from_tsv_gives_the_example_hash(self):
        ref = _reference()
        example = _example()
        parsed = ref["parse_archive_listing"](_listing_tsv(example["listing"]))
        self.assertEqual(ref["selection_sha256"](example["triage"], parsed), _fence("text selection-example-sha256").strip())

    def test_parsing_keeps_the_first_row_merges_in_order_and_refuses_a_bad_size(self):
        ref = _reference()
        first = b"archive\tpath\tsize\tcrc32\nA.zip\tx/S01.mzML\t5\t1\nA.zip\tX/s01.mzml\t9\t2\n"
        second = b"archive\tpath\tsize\tcrc32\nB.zip\tx/S01.mzML\t7\t3\nB.zip\ty/S02.mzML\t8\t4\n"
        merged = ref["parse_archive_listing"](first)
        ref["parse_archive_listing"](second, merged)
        self.assertEqual(merged, {"x/s01.mzml": {"archive": "A.zip", "crc32": "1", "size": 5},
                                  "y/s02.mzml": {"archive": "B.zip", "crc32": "4", "size": 8}})
        with self.assertRaises(ValueError):
            ref["parse_archive_listing"](b"archive\tpath\tsize\tcrc32\nA.zip\tx.mzML\t1.5\t1\n")
        _says(_section("### 3.1"), "merged in record order", "the first row of a path wins")

    def test_added_members_are_in_the_selection(self):
        ref = _reference()
        triage = {"unit_id": "AN000001", "rows": [{"row_index": 1, "member_path": "a/S01.mzML"}],
                  "added_members": [{"member_path": "a/QC01.mzML"}]}
        self.assertEqual(len(ref["selection_canonical"](triage, {})), 2)

    def test_a_child_keeps_the_bare_unit_id_and_its_own_members(self):
        ref = _reference()
        triage = {"unit_id": "AN000001",
                  "rows": [{"row_index": 1, "member_path": "pos/S01.mzML"}, {"row_index": 2, "member_path": "neg/S01.mzML"}],
                  "children": [{"child_key": "pos", "member_paths": ["POS/s01.mzml"]}]}
        entries = ref["selection_canonical"](triage, {}, triage["children"][0])
        self.assertEqual([(entry["unit_id"], entry["child_key"], entry["member_path"]) for entry in entries],
                         [("AN000001", "pos", "pos/S01.mzML")])

    def test_crc32_and_size_come_from_the_listing_never_the_triage(self):
        ref = _reference()
        bare = {"unit_id": "AN000001", "rows": [{"row_index": 1, "member_path": "a/S01.mzML"}]}
        claimed = {"unit_id": "AN000001", "rows": [{"row_index": 1, "member_path": "a/S01.mzML",
                                                     "crc32": "DEADBEEF", "size": 99}]}
        self.assertEqual(ref["selection_sha256"](bare, {}), ref["selection_sha256"](claimed, {}))
        listing = {"a/s01.mzml": {"crc32": "1", "size": "7"}}
        entry = ref["selection_canonical"](claimed, listing)[0]
        self.assertEqual((entry["crc32"], entry["size"]), ("00000001", 7))

    def test_the_hash_is_order_independent_and_canonical(self):
        ref = _reference()
        rows = [{"row_index": index, "member_path": f"a/S{index:02d}.mzML"} for index in range(1, 5)]
        forward = {"unit_id": "AN000001", "rows": rows}
        backward = {"unit_id": "AN000001", "rows": list(reversed(rows))}
        self.assertEqual(ref["selection_sha256"](forward, {}), ref["selection_sha256"](backward, {}))
        payload = ref["canonical_json"](ref["selection_canonical"](forward, {}))
        self.assertEqual(ref["selection_sha256"](forward, {}), hashlib.sha256(payload).hexdigest())
        self.assertNotIn(b" ", payload)

    def test_the_reference_takes_a_child_key_as_the_implementations_do(self):
        """WS0 low (2026-11-17): the reference accepts a child_key string, "" as the whole unit, raises ValueError for
        an unknown key and TypeError for a string listing, as section 3.1 states and Interactive and the runner do."""
        ref = _reference()
        for triage, listing, child in _cases():
            if child is None:
                continue
            with self.subTest(child=child["child_key"], listing=bool(listing)):
                self.assertEqual(ref["selection_sha256"](triage, listing, child["child_key"]),
                                 ref["selection_sha256"](triage, listing, child))
        example = _example()
        self.assertEqual(ref["selection_sha256"](example["triage"], example["listing"], ""),
                         _fence("text selection-example-sha256").strip())
        with self.assertRaises(ValueError):
            ref["selection_canonical"](_cases()[2][0], {}, "no-such-child")
        with self.assertRaises(TypeError):
            ref["selection_canonical"](example["triage"], "pos")
        _says(_section("### 3.1"), "The reference and the implementations also accept a child's `child_key` string",
              "an unknown key raises `ValueError`", "a string passed as the listing raises `TypeError`")

    def test_the_runner_compares_the_value_interactive_recorded(self):
        _says(_section("### 2.5"), "holds the unit when they differ (`contract_held`, `curated_selection_selection_mismatch`)",
              "refuses the unit with `curated_selection_selection_mismatch`")


def _handoff(**changes) -> dict:
    """A synthetic Catalog-shaped handoff: no Class saved, so the builder's Class code holds it (section 2.5)."""
    handoff = {
        "analysis_unit_id": "AN000001", "accession": "ST000001",
        "files": [{"name": "S01.mzML", "bytes": 10}, {"name": "S02.mzML", "bytes": 11}],
        "samples": [{"sample_id": "S01", "factors": "A"}, {"sample_id": "S02", "factors": "B"}],
        "technical_metadata": {"ion_mode": "Positive"}, "required_review": [],
        "class_proposal": None, "blocking_reasons": ["class_proposal:missing"],
        "next_action": "save_class_proposal", "ready_for_download_planning": False,
    }
    handoff.update(changes)
    return handoff


def _ratified(campaign: str) -> dict:
    return {"proposal_id": "cp-1", "unit_id": "AN000001", "status": "accepted", "ratified_by": f"approval-{campaign}",
            "assignments": [{"sample_id": "S01", "class": "A"}, {"sample_id": "S02", "class": "B"}],
            "ratification": {"proposal_id": "cp-1", "approval_id": f"approval-{campaign}", "campaign_id": campaign}}


def _class_cases() -> list[tuple[str, dict, dict, list[str]]]:
    """(name, triaged handoff, handoff taken now, the keys section 2.5 says differ)."""
    ready = {"blocking_reasons": [], "next_action": "plan_download", "ready_for_download_planning": True}
    confirmed = {**_ratified("a"), "ratified_by": None, "ratification": None}
    ion = "technical_metadata:ion_mode"
    return [
        ("triaged with no Class, then ratified", _handoff(), _handoff(class_proposal=_ratified("b"), **ready), []),
        ("confirmed but unratified, then ratified", _handoff(class_proposal=confirmed, **ready),
         _handoff(class_proposal=_ratified("b"), **ready), []),
        ("re-ratified", _handoff(class_proposal=_ratified("a"), **ready),
         _handoff(class_proposal=_ratified("b"), **ready), []),
        ("a changed file list", _handoff(), _handoff(class_proposal=_ratified("b"), **ready,
                                                     files=[{"name": "S01.mzML", "bytes": 10}]), ["files"]),
        ("a changed sample row", _handoff(), _handoff(samples=[{"sample_id": "S01", "factors": "B"}]), ["samples"]),
        ("a changed unit id", _handoff(), _handoff(analysis_unit_id="AN000002"), ["analysis_unit_id"]),
        ("a non-Class code appearing", _handoff(),
         _handoff(class_proposal=_ratified("b"), blocking_reasons=[ion], next_action="review_technical_metadata",
                  ready_for_download_planning=False), ["blocking_reasons", "next_action"]),
        ("a non-Class code disappearing", _handoff(blocking_reasons=["class_proposal:missing", ion]),
         _handoff(class_proposal=_ratified("b"), **ready),
         ["blocking_reasons", "next_action", "ready_for_download_planning"]),
        ("an unknown class_proposal code", _handoff(),
         _handoff(blocking_reasons=["class_proposal:missing", "class_proposal:withdrawn"]), ["blocking_reasons"]),
    ]


def _interactive():
    root = os.environ.get("MSDIAL_INTERACTIVE_ROOT")
    if not root:
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        module = importlib.import_module("msdial_app.curated_selection")
    except ImportError:
        return None
    return module if hasattr(module, "selection_canonical") else None


def _runner_copy():
    path = ROOT / "scripts" / "campaign" / "curated.py"
    if not path.is_file():
        return None
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        module = importlib.import_module("campaign.curated")
    except ImportError:
        return None
    return module if hasattr(module, "selection_canonical") else None


class ImplementationsAgree(unittest.TestCase):
    """WS0 decision 1: the doc's reference, Interactive's selection_canonical and the runner's copy give one value."""

    def _compare(self, other) -> None:
        ref = _reference()
        self.assertEqual(other.SELECTION_CANONICAL_FORM, ref["SELECTION_CANONICAL_FORM"])
        for triage, listing, child in _cases():
            with self.subTest(unit=triage["unit_id"], child=(child or {}).get("child_key"), listing=bool(listing)):
                self.assertEqual(other.selection_canonical(triage, listing, child),
                                 ref["selection_canonical"](triage, listing, child))
                self.assertEqual(other.selection_sha256(triage, listing, child),
                                 ref["selection_sha256"](triage, listing, child))
        data = _listing_tsv(_example()["listing"])
        self.assertEqual(other.parse_archive_listing(data), ref["parse_archive_listing"](data))
        # The child given by its key, the whole unit as "", and the two errors (section 3.1).
        for triage, listing, child in _cases():
            key = "" if child is None else child["child_key"]
            with self.subTest(unit=triage["unit_id"], child_key=key, listing=bool(listing)):
                self.assertEqual(other.selection_sha256(triage, listing, key), ref["selection_sha256"](triage, listing, key))
        split = _cases()[2][0]
        for function in (other.selection_canonical, ref["selection_canonical"]):
            with self.assertRaises(ValueError):
                function(split, {}, "no-such-child")
            with self.assertRaises(TypeError):
                function(split, "pos")

    def _compare_class_keys(self, other) -> None:
        """WS0 decision F: the implementation leaves out exactly the set section 2.5 names, and decides the cases the
        section states as it states them."""
        keys = getattr(other, "HANDOFF_CLASS_KEYS", None)
        self.assertIsNotNone(keys, "the implementation names no HANDOFF_CLASS_KEYS (WS0 decision F)")
        self.assertEqual({name: tuple(value) for name, value in keys.items()}, HANDOFF_CLASS_KEYS)
        self.assertFalse(hasattr(other, "HANDOFF_RATIFICATION_KEYS"), "decision A's ratification-only set is gone")
        for name, original, copy, expected in _class_cases():
            with self.subTest(case=name):
                differing, record = other.handoff_differing_keys(original, copy)
                self.assertEqual(sorted(differing), sorted(expected))
                self.assertEqual(record["excluded_keys"], list(HANDOFF_CLASS_KEYS["keys"]))
                self.assertEqual(record["excluded_blocking_reason_codes"],
                                 list(HANDOFF_CLASS_KEYS["blocking_reason_codes"]))

    def test_interactive_gives_the_reference_value(self):
        module = _interactive()
        if module is None:
            self.skipTest("MSDIAL_INTERACTIVE_ROOT names no Interactive with msdial-selection-canonical.v1")
        self._compare(module)
        self._compare_class_keys(module)
        replay = getattr(module, "replay_projection", None)
        if replay is not None:
            self.assertEqual(sorted(replay({})), sorted(PROJECTION_V1_KEYS))

    def test_the_runner_copy_gives_the_reference_value(self):
        module = _runner_copy()
        if module is None:
            self.skipTest("this checkout holds no scripts/campaign/curated.py with msdial-selection-canonical.v1")
        self._compare(module)
        self._compare_class_keys(module)
        self.assertEqual(tuple(module.DECISION_KINDS), LEDGER_KINDS)
        self.assertEqual({kind: tuple(fields) for kind, fields in module.REVIEW_LOG_KINDS.items()}, REVIEW_LOG_KINDS)


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

    def test_record_inputs_resolve_beside_the_record(self):
        _says(_section("### 2.3"), "relative to the record file's own directory", "`curated_selection_record_mismatch`",
              "`triage_record_mismatch`")


class RunTimeLayout(unittest.TestCase):
    """WS0 decisions 2, 5 and 7: the copies, the record Interactive reads, the handoff identity, the workspace key."""

    def test_the_copies_are_laid_out_as_interactive_looks_for_them(self):
        section = _section("### 2.1")
        for name in ("handoffs\\<unit_key>\\", "handoff.original.json", "<unit_id>.json", "<unit_id>.record.json"):
            self.assertIn(name, section)
        _says(section, "passes the original as `decision_record_path`", "`<unit_key>@<campaign_id>` for a reopened unit")

    def test_the_download_arguments_are_the_ones_interactive_takes(self):
        section = _section("### 2.5")
        for argument in DOWNLOAD_ARGUMENTS:
            self.assertIn(f"| `{argument}` |", section, argument)
        _says(section, "`curated_selection_paths` and `workspace_unit_keys`", "`verify_handoff_copy`",
              "`handoff_file`", "`original_read_and_copy_compared`", "never passed because its rows look the same")
        self.assertNotIn("runner_asserted", _text())

    def test_the_workspace_key_rule_is_interactives(self):
        _says(_section("### 2.5"), f"`{WORKSPACE_UNIT_KEY}`", "`project.repository_metadata.workspace_unit_key`",
              "followed by one of `- _ . @ +`")

    def test_the_applied_selection_is_read_from_the_project(self):
        section = _section("### 2.5")
        _says(section, "under `project.repository_metadata.curated_selection` (`msdial-curated-selection.v1`)",
              "fall back to an older top-level `curated_selection`")
        self.assertNotIn("under `repository_metadata.curated_selection`", _text())
        for key in ("selection_sha256_matches_manifest", "catalog_handoff_verification", "triage_record",
                    "selection_listing_entries", "added_members_not_admitted", "applied_by"):
            self.assertIn(f"`{key}`", section, key)


class DecisionLedger(unittest.TestCase):
    """Finding 3 and WS0 decision 8: the campaign's decision-ledger.jsonl is named, mapped, checked and released; the
    review log carries only its kinds."""

    def test_every_runner_ledger_kind_is_mapped(self):
        section = _section("### 2.4")
        self.assertIn("decision-ledger.jsonl", section)
        for kind in LEDGER_KINDS:
            self.assertIn(f"| `{kind}` |", section, kind)

    def test_every_review_log_kind_has_exactly_its_fields(self):
        section = _section("### 2.4")
        for kind, fields in REVIEW_LOG_KINDS.items():
            cell = _table_row(section, kind).split("|")[2]
            named = re.findall(r"`([a-z_0-9]+)`", cell)
            for field in fields:
                self.assertIn(field, named, f"{kind}: {field}")

    def test_operator_requests_and_verdicts_stay_out_of_the_review_log(self):
        _says(_section("### 2.4"), "refuses any other kind and any field outside its kind's row",
              "`operator_request` campaign event", "not exported to the decision ledger",
              "Gate and header verdicts never enter the review log", "`--question-code` and a named `--by`")

    def test_ledger_paths_are_never_absolute(self):
        _says(_section("### 2.4"), "a ledger line never holds an absolute path")

    def test_the_verifier_walks_the_ledger(self):
        self.assertIn("walks every review log and the ledger", _section("### 2.4"))

    def test_the_ledger_is_released(self):
        self.assertIn("decision-ledger.jsonl", _section("### 5.3"))


class ReplayLevels(unittest.TestCase):
    """Findings 4 and 5, PR 59 review findings on the inputs level and the bundle, WS0 decision 9."""

    def test_the_inputs_level_follows_the_projection_schema(self):
        section = _section("### 3.2")
        self.assertIn("msdial-replay-projection.v1", section)
        _says(section, "It does not cover libraries, the Console identity or the parameter file")
        self.assertIn("msdial-parameter-file-canonical.v1", section)
        self.assertNotIn("`parameter_file_sha256`", _text())

    def test_the_projection_fields_are_interactives(self):
        section = _section("### 3.2")
        for key in PROJECTION_V1_KEYS:
            self.assertIn(f"`{key}`", section, key)
        _says(section, "`finalize_download_lease`", "Nothing writes them earlier, so this hash needs a Console run")

    def test_what_needs_a_console_run_is_stated(self):
        section = _section("## 3.")
        inputs = next(line for line in section.splitlines() if line.startswith("| `inputs` |"))
        self.assertIn("needs a Console run", inputs)
        _says(section, "What needs a Console run", "`inputs` row is therefore not evaluable as a whole",
              "the zero-threshold diagnostic", "`curated_selection.manifest_digest`")
        self.assertIn("{n_inputs_equal}", _section("### 5.1"))
        _says(_section("### 5.1"), "`members` and `analysis_csv.canonical_sha256` checks are both `equal`")

    def test_the_digest_check_from_released_bytes_is_stated(self):
        section = _section("## 3.")
        self.assertNotIn("the manifest digest recomputes", section)
        _says(section, "`original_sha256` must be its `approved_manifest_digest`", "`release_sha256`",
              "can be recomputed only on the producing machine")

    def test_the_cli_is_the_scripts(self):
        _says(_section("## 3."), "[--unit <unit key>] [--libraries <dir>] [--out <report>] [--json]",
              "`msdial-campaign-replay-report.v1`", "`replay-inputs\\<unit key>.json`")

    def test_the_outputs_level_is_compared_after_finalisation(self):
        section = _section("### 3.3")
        self.assertIn("ms_run[n]-location", section)
        self.assertIn("database[n]-uri", section)
        self.assertIn("never released", section)
        _says(section, "`units/<unit key>/output-checksums.tsv`", "does not write this file yet")


class RevisionsAndAnswers(unittest.TestCase):
    """Findings 7 and 8: answers are read from the review log by code; a validator error redo has a cause."""

    def test_v2_reads_person_answers_from_the_review_log_by_code(self):
        section = _section("### 2.2")
        self.assertIn("reads each accession's `review-log.jsonl`", section)
        self.assertIn("matching `question_code`", section)
        _says(_section("### 2.4"), "without a `person_answer` of the matching `question_code`")

    def test_a_validator_verdict_is_an_admissible_revision_cause(self):
        row = _table_row(_section("### 2.4"), "revision")
        self.assertIn("`validator_verdict`", row)
        self.assertIn("`<unit_id>.r<N>.json`", _section("## 6."))


class HeaderReconciliation(unittest.TestCase):
    """Finding 9 and WS0 decision 3: the scope, shape and code are what Interactive builds."""

    def test_the_code_and_scope_match_interactive(self):
        text = _text()
        self.assertNotIn("folder_header_disagreement", text)
        section = _section("## 6.")
        self.assertIn("triage_header_disagreement", section)
        _says(section, "MS levels, DIA window signature and run end time are not compared",
              "ion_mode: {triage, triage_basis, header, result}", "`msdial-curated-selection-reconciliation.v1`",
              "`not_read`", '{file, member_path: null, result: "disagree", reason: "not_in_selection", '
              'reasons: ["not_in_selection"]}', "`curated_selection_reconciliation_not_recorded`")

    def test_the_items_go_to_the_ledger_only(self):
        _says(_section("## 6."), "into the ledger's `header_reconciliation` entry", "Nothing goes to the review log")


class CrossUnitGate(unittest.TestCase):
    """PR 59 review finding on rule S and WS0 decisions 3 and 6: the four rules, the vocabulary, a single member."""

    def test_the_four_rules_are_named_everywhere(self):
        text = _flat(_text())
        self.assertNotIn("rules P, C and D", text)
        self.assertNotIn("rule P, C or D", text)
        self.assertNotIn("rules P, C, D)", text)
        _says(_section("## 1."), "rules P, C, S and D")
        section = _section("## 6.")
        for rule, code in CROSS_UNIT_RULES.items():
            _says(section, f"**{rule}** (`{code}`)")
        _says(section, "`cross_unit_attribution_2026_11_16b`", "`containment_minimum` 0.5", "Both units are refused")

    def test_the_runner_reads_interactives_vocabulary(self):
        _says(_section("## 6."), "`verdict` `passed` or `refused`", "the codes in `reasons`", "`cross_unit_reply_unreadable`",
              "`not_applicable` with reason `single_member`")
        row = _table_row(_section("### 2.4"), "cross_unit_verdict")
        for phrase in ("`not_applicable`", "`single_member`", "`cross_unit_attribution_2026_11_16b`", "`version`",
                       "P, C, S or D"):
            self.assertIn(phrase, row)


class Release(unittest.TestCase):
    """Finding 10 and the PR 59 review finding on the bundle: the files the replay reads, and the redaction rule."""

    def test_manifests_are_released_as_a_projection(self):
        section = _section("### 5.3")
        self.assertIn("Release projection", section)
        self.assertIn("release-index.json", section)

    def test_the_bundle_is_what_the_release_command_writes(self):
        section = _section("### 5.3")
        for name in ("release-index.json", "campaign-manifest.release.json", "units\\<unit key>\\run-manifest.release.json",
                     "triage\\<accession>\\", "decision-ledger.jsonl", "msdial-release-index.v1"):
            self.assertIn(name, section, name)
        _says(section, "`campaign-runner.py release --campaign <id> --out <empty folder>`",
              "{schema, approved_manifest_digest, manifests: [{name, release, original_sha256, release_sha256}], "
              "triage_inputs: [{name, release, original_sha256, release_sha256, projection}]}",
              "the original manifests, `campaign-authorization.json`")

    def test_the_redaction_rule_covers_unc_paths_and_context_blocks(self):
        section = _section("### 5.3")
        self.assertIn("UNC path", section)
        _says(section, "context file or memory")


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


class WS0DecisionsOf20261117(unittest.TestCase):
    """The WS0 decisions of 2026-11-17 as Interactive #102 39f42f2 and runner #60 f25e5b8 built them."""

    def test_a_f_the_triage_binds_evidence_and_the_class_binds_by_ratification(self):
        """WS0 decision F (Interactive #102 0c2921a, runner #60 87c4fe3), superseding decision A's ratification-only
        scope: the triage runs before the Class, binds the evidence it read, and never cites the Class; the Class binds
        through its own ratification and the class digest."""
        order = _section("### 2.1")
        _says(order, "The triage runs first, read only, before any Class proposal of the unit is saved",
              "never reads or cites the handoff's `class_proposal`", "(error `T-CLASS`)",
              "the triage binds its evidence through `catalog_handoff_sha256` and the handoff comparison",
              "the Class binds through its own ratification and the class digest", "handoff.triaged.json")
        section = _section("### 2.5")
        for code in HANDOFF_CLASS_KEYS["blocking_reason_codes"]:
            self.assertIn(f"`{code}`", section, code)
        for key in HANDOFF_CLASS_KEYS["readiness_keys"]:
            self.assertIn(f"`{key}`", section, key)
        _says(section, "`curated_selection.HANDOFF_CLASS_KEYS`", "`HANDOFF_CLASS_KEYS`", "the whole `class_proposal`",
              "any other code appearing or disappearing",
              "only when the remaining (non Class) `blocking_reasons` are equal on both sides",
              "Everything else is compared: the files, the sample rows, the technical settings, the sidecars' contents, "
              "the unit and accession", "`curated.class_binding`", "`class_not_ratified`", "`class_digest_changed`",
              "a byte identical handoff included", "`handoff_identity: class_only`", "`triaged_bytes_found: false`",
              "never passed because its rows look the same",
              "`class` `{excluded_keys, excluded_blocking_reason_codes, readiness_keys, readiness_left_out, original, "
              "copy, differs}`", "Interactive does not check that it was ratified under the campaign")
        row = _table_row(section, "original_handoff_path")
        self.assertIn("handoff.original.json", row)
        self.assertIn("handoff.triaged.json", row)
        ledger = _section("### 2.4")
        for phrase in ("`class_only`", "`class_binding`", "`class_not_ratified`", "`original_handoff.class`"):
            self.assertIn(phrase, _table_row(ledger, "triage_checked"), phrase)
        self.assertIn("`handoff_class`", _table_row(ledger, "curated_selection_applied"))
        _says(_section("## 6."), "in more than what the Class decides", "`class_not_ratified`")
        _says(_section("## 7."), "the triage first, read only and before any Class is saved", "WS0 decision F",
              "No unit is triaged again because its Class was ratified", "(PR #102, 0c2921a)", "(PR #60, cfb8b39)",
              "`T-CLASS`")
        text = _flat(_text())
        for gone in ("HANDOFF_RATIFICATION_KEYS", "ratification_only", "handoff_ratification",
                     "Triage from a handoff taken after the unit's Class proposal was ratified",
                     "whether a handoff whose ratification appears where the triaged handoff had none may pass",
                     "must be the triaged one byte for byte", "differs only in its ratification"):
            self.assertNotIn(gone, text, gone)

    def test_b_a_unit_refused_after_it_ran_is_invalidated(self):
        section = _section("## 6.")
        _says(section, f"`{INVALIDATED_AFTER_DONE}`", f"`{INVALIDATION_QUESTION}`", "`held_before_download`",
              "has a unit manifest or has ended before its download", "`refused_after_pass`",
              "Its outputs and raw data are kept. The runner never deletes them", "`outputs_valid`",
              "The Catalog run record has status `invalidated_after_done`", "`plan --reopen`")
        self.assertNotIn("withdrawn from redistribution. The refusal", _flat(section))
        self.assertNotIn("Units an earlier check passed are sent first", _flat(section))
        row = _table_row(_section("### 2.4"), "terminal_refusal")
        for phrase in ("`invalidated_after_done: true`", "`outputs: kept_flagged_invalid`",
                       f"`question_code: {INVALIDATION_QUESTION}`"):
            self.assertIn(phrase, row)
        self.assertIn("`refused_after_pass`", _table_row(_section("### 2.4"), "cross_unit_verdict"))

    def test_c_triage_inputs_are_released_as_projections(self):
        section = _section("### 5.3")
        _says(section, "triage_inputs: [{name, release, original_sha256, release_sha256, projection}]",
              "`projection: release_projection`", "the bundle is not written",
              "The original handoff's sha256 stays verifiable without its bytes",
              "the hash chained ledger's `triage_recorded` `catalog_handoff_sha256`",
              "The original bytes, with their absolute paths, stay on the producing machine and are not released",
              "`curated.release_redaction_findings`", "gets no `release-index.json`")
        self.assertNotIn("copied whole", section)
        _says(_section("## 3."), "`curated.released_catalog`", "`released_triage_inputs` check `different`")
        _says(_section("### 2.3"), "checked through `release-index.json` instead")

    def test_d_a_stated_role_overrides_the_name_filter(self):
        _says(_section("## 6."), "a row whose role is `sample` is compared whatever its file name or sample id says",
              "a row whose role is `qc`, `blank` or `other` is not compared",
              "only a row whose role is `unknown` or empty, or an input no curated row names, is read by its names")

    def test_e_the_lows(self):
        section = _section("## 6.")
        _says(section, "So does a curated unit that is the only one in its download group, once",
              "Interactive holds the unit until a revised triage is planned again",
              "Accepting the triage against the header has no path yet", "are future work (section 7)",
              "`class_digest_changed`")
        self.assertNotIn("The person accepts the header, accepts the triage or excludes the file", _flat(section))
        _says(_section("### 2.5"), "`curated.workspace_name_problem`", "refused at plan time, never at the download")
        _says(_section("### 2.4"), "once for a curated unit alone in its group")
        self.assertIn("class_digest_changed", _table_row(_section("### 2.4"), "terminal_refusal"))
        _says(_section("## 7."), "Future work:", "accept the header, accept the triage, exclude the file")

    def test_f_a_copy_with_no_class_key_and_a_copy_changed_after_its_check_are_held(self):
        """The round-6 low (runner #60 cfb8b39): a handoff copy with no class_proposal key is no Class, held as
        class_not_ratified and never bound; and the download start hashes the copy and its sidecars again, holding a
        copy changed since its Class was checked (handoff_copy_changed)."""
        section = _section("### 2.5")
        _says(section, "a copy without it is held the same way (`class_not_ratified`, with `class_proposal_key: false`)",
              "and never bound", "`copy_sha256`", "(`contract_held`, `handoff_copy_changed`, naming the file)",
              "its recheck takes the Catalog handoff again (from `class_settled`)")
        self.assertIn("`handoff_copy_changed`", _table_row(_section("### 2.4"), "triage_checked"))
        _says(_section("## 6."), "or a copy with no `class_proposal` key at all",
              "(`contract_held`, `handoff_copy_changed`)")
        _says(_section("## 7."), "(`handoff_copy_changed`)")
        text = _flat(_text())
        for gone in ("checked: false", "is recorded with `checked", "not the Catalog's shape"):
            self.assertNotIn(gone, text, gone)


if __name__ == "__main__":
    unittest.main()
