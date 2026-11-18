"""scripts/validate-triage.py v2: the msdial-unit-triage.v2 payload, its decision record and the review log.

docs/llm-assisted-curation.md sections 2.2 to 2.4 and 5.3. The model writes only the payload (v2: a confidence and a
rationale per decision, no person_answers); the wrapper writes the envelope (msdial-llm-decision-record.v1) from the
harness transcript; a person's answer is a review-log entry. The validator recomputes every hash the envelope names,
checks the tool calls against the skill's allowed-tools, holds what is not clean or not settled, and reads the
review log for the answers that let a flagged unit be ready. Every fixture here is synthetic, with public accession
ids only.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "scripts"))
import decision_records as records  # noqa: E402
import triage_eval as te  # noqa: E402
from campaign import curated  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("validate_triage_v2_under_test", ROOT / "scripts" / "validate-triage.py")
assert _SPEC and _SPEC.loader
vt = importlib.util.module_from_spec(_SPEC)
sys.modules["validate_triage_v2_under_test"] = vt
_SPEC.loader.exec_module(vt)

from test_validate_triage import EXPECTED  # noqa: E402

# The skill's allowed tools, exactly (SKILL.md front matter); the validator checks tool_calls against them.
ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Write",
    "mcp__msdial-triage-spec__triage_spec_check",
]
CHECK = "mcp__msdial-triage-spec__triage_spec_check"
AT = "2026-11-18T00:00:00+00:00"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class V2Case(unittest.TestCase):
    ACCESSION = "ST002747"  # four ready units, every row declared or named; passes as v1

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.code = records.Code.from_root(self.tmp / "code")
        self._n = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make(self, accession: str | None = None, docs: dict | None = None, *, record: bool = True,
             **record_options) -> Path:
        """A triage directory of v2 payloads, each with a complete decision record binding the archive listing."""
        accession = accession or self.ACCESSION
        self._n += 1
        docs = docs if docs is not None else te.to_v2(te.build_good(accession))
        directory = te.write(docs, self.tmp / f"{accession}-{self._n}")
        bound = records.copy_listing(te.load(accession)[2], directory)
        if record:
            for unit in docs:
                records.write_record(directory / f"{unit}.json", self.code, inputs=[bound], **record_options)
        return directory

    def validate(self, directory: Path, accession: str | None = None, **options) -> "vt.Report":
        accession = accession or self.ACCESSION
        options.setdefault("code_root", self.code.root)
        return vt.validate(directory, vt.read_catalog(te.fixture_dir(accession) / "catalog.json"),
                           vt.read_listing(te.load(accession)[2]), **options)

    @staticmethod
    def found(report, check: str, level: str | None = None, unit: str | None = None) -> list[str]:
        return [f.message for f in report.findings
                if f.check == check and (level is None or f.level == level) and (unit is None or f.unit == unit)]

    def rewrite_record(self, directory: Path, unit: str, change) -> None:
        path = directory / f"{unit}.record.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        change(value)
        path.write_text(json.dumps(value, indent=1), encoding="utf-8")


class SchemaTests(V2Case):
    def test_the_schemas_are_valid_draft_2020_12(self) -> None:
        try:
            import jsonschema  # noqa: PLC0415
        except ImportError:
            self.skipTest("jsonschema is not installed")
        for schema_id in (vt.SCHEMA_V2_ID, vt.RECORD_SCHEMA_ID):
            jsonschema.Draft202012Validator.check_schema(vt.load_schema(schema_id))

    def test_v2_is_v1_plus_confidence_and_rationale_without_person_answers(self) -> None:
        v1, v2 = vt.load_schema(vt.SCHEMA_ID), vt.load_schema(vt.SCHEMA_V2_ID)
        self.assertEqual(v2["properties"]["schema"], {"const": "msdial-unit-triage.v2"})
        self.assertNotIn("person_answers", v2["properties"])
        self.assertEqual(set(v2["required"]) - set(v1["required"]), {"confidence", "rationale"})
        for name in ("row", "member", "excluded_row", "excluded_member"):
            self.assertTrue({"confidence", "rationale"} <= set(v2["$defs"][name]["required"]), name)
        self.assertEqual(v2["$defs"]["confidence"]["enum"], ["high", "medium", "low"])
        record = vt.load_schema(vt.RECORD_SCHEMA_ID)
        self.assertEqual(record["properties"]["schema"], {"const": "msdial-llm-decision-record.v1"})
        self.assertEqual(set(record["required"]), {
            "schema", "decision_id", "kind", "accession", "unit_id", "payload", "supersedes", "inputs", "skill", "wrapper",
            "prompt",
            "agent", "started_at", "finished_at", "tool_calls", "transcript", "rationale"})
        self.assertEqual(record["properties"]["kind"]["enum"],
                         ["unit_triage", "metadata_interpretation", "class_proposal", "question"])

    def test_every_eval_accession_reaches_its_expected_verdict_as_v2(self) -> None:
        for accession, (verdict, statuses) in EXPECTED.items():
            with self.subTest(accession):
                directory = self.make(accession)
                report = self.validate(directory, accession)
                self.assertEqual(report.verdict, verdict, [f.as_dict() for f in report.findings if f.level != "note"])
                for unit, status in statuses.items():
                    self.assertEqual(report.unit_status[unit], status)
                self.assertFalse(self.found(report, "T-RATIONALE"))
                self.assertFalse([f for f in report.findings if f.check in ("T-RECORD", "T-TOOLS", "T-CONTEXT")],
                                 [f.as_dict() for f in report.findings])

    def test_v1_stays_readable_and_is_reported_rationale_not_recorded(self) -> None:
        directory = te.write(te.build_good(self.ACCESSION), self.tmp / "v1")
        report = self.validate(directory)
        self.assertEqual(report.verdict, "pass")
        notes = self.found(report, "T-RATIONALE", "note")
        self.assertEqual(len(notes), 4)
        self.assertTrue(all(note.startswith("rationale_not_recorded") for note in notes))
        self.assertEqual(report.as_dict()["units"]["AN004454"]["schema"], vt.SCHEMA_ID)

    def test_a_v2_payload_without_confidence_or_with_person_answers_is_refused(self) -> None:
        for label, change in (
                ("no confidence on a row", lambda d: d["rows"][0].pop("confidence")),
                ("no rationale on an exclusion", lambda d: d["excluded_rows"][0].pop("rationale")),
                ("no unit rationale", lambda d: d.pop("rationale")),
                ("a confidence outside the scale", lambda d: d["rows"][0].update(confidence="certain")),
                ("person_answers in the payload", lambda d: d.update(person_answers=[
                    {"by": "A Person", "answered_at": AT, "code": "ms1_only", "answer": "yes"}]))):
            with self.subTest(label):
                docs = te.to_v2(te.build_good(self.ACCESSION))
                change(docs["AN004454"])
                report = self.validate(self.make(docs=docs))
                self.assertTrue(self.found(report, "T-SCHEMA", "error", "AN004454"), label)

    def test_an_unknown_schema_is_refused(self) -> None:
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs["AN004454"]["schema"] = "msdial-unit-triage.v3"
        report = self.validate(self.make(docs=docs, record=False))
        self.assertTrue(self.found(report, "T-SCHEMA", "error", "AN004454"))


class ConfidenceTests(V2Case):
    def test_an_inferred_low_confidence_file_holds_a_ready_unit(self) -> None:
        docs = te.to_v2(te.build_good(self.ACCESSION))
        row = docs["AN004454"]["rows"][0]
        self.assertEqual(row["basis"], "inferred")
        row["confidence"] = "low"
        directory = self.make(docs=docs)
        report = self.validate(directory)
        self.assertEqual(report.unit_verdict("AN004454"), "hold")
        self.assertTrue(self.found(report, "T-CONFIDENCE", "hold", "AN004454"))
        self.assertEqual(report.unit_status["AN004454"], "ready")
        # A person who looked releases it: a person_answer about this payload with the code inferred_low_confidence.
        payload = sha((directory / "AN004454.json").read_bytes())
        curated.append_review_log(directory, "person_answer", payload, AT, by="Synthetic Reviewer",
                                  question_code=vt.LOW_CONFIDENCE_CODE, answer="The name is unambiguous; keep it.",
                                  channel="conversation")
        report = self.validate(directory)
        self.assertFalse(self.found(report, "T-CONFIDENCE"))
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])

    def test_low_confidence_alone_or_outside_a_ready_unit_does_not_hold_for_it(self) -> None:
        # Declared at low confidence: the rule is inferred and low.
        docs = te.to_v2(te.build_good("ST004606"))
        row = docs["AN007766"]["rows"][0]
        self.assertEqual(row["basis"], "declared")
        row["confidence"] = "low"
        report = self.validate(self.make("ST004606", docs), "ST004606")
        self.assertFalse(self.found(report, "T-CONFIDENCE"))
        self.assertEqual(report.verdict, "pass")
        # An inferred exclusion or a unit that is not ready is not what the rule holds.
        docs = te.to_v2(te.build_good("ST002798"))
        for unit in docs.values():
            for item in unit["rows"]:
                item["confidence"] = "low"
        report = self.validate(self.make("ST002798", docs), "ST002798")
        self.assertFalse(self.found(report, "T-CONFIDENCE"))


class RecordTests(V2Case):
    UNIT = "AN004454"

    def test_a_complete_record_passes(self) -> None:
        report = self.validate(self.make())
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])

    def test_a_v2_payload_without_a_record_holds(self) -> None:
        report = self.validate(self.make(record=False))
        self.assertEqual(report.verdict, "hold")
        self.assertEqual(len(self.found(report, "T-RECORD", "hold")), 4)

    def test_every_hash_the_record_names_is_recomputed(self) -> None:
        unit = self.UNIT

        def tamper_file(relative: str):
            def change(directory: Path) -> None:
                path = directory / relative
                path.write_bytes(path.read_bytes() + b"\n")
            return change

        def record_change(change):
            return lambda directory: self.rewrite_record(directory, unit, change)

        cases = {
            "decision_id": record_change(lambda r: r.update(decision_id="0" * 64)),
            "payload.sha256": record_change(lambda r: r["payload"].update(sha256="0" * 64)),
            "payload.path": record_change(lambda r: r["payload"].update(path="AN004455.json")),
            "payload.schema": record_change(lambda r: r["payload"].update(schema=vt.SCHEMA_ID)),
            "the unit": record_change(lambda r: r.update(unit_id="AN004455")),
            "kind": record_change(lambda r: r.update(kind="class_proposal")),
            "an input's sha256": record_change(lambda r: r["inputs"][0].update(sha256="0" * 64)),
            "an input's bytes": record_change(lambda r: r["inputs"][0].update(bytes=r["inputs"][0]["bytes"] + 1)),
            "an input changed": tamper_file("inputs/archive-members.tsv"),
            "an input missing": lambda directory: (directory / "inputs" / "archive-members.tsv").unlink(),
            "an absolute input path": record_change(lambda r: r["inputs"][0].update(path="D:/somewhere/archive-members.tsv")),
            "skill.sha256": record_change(lambda r: r["skill"].update(sha256="0" * 64)),
            "skill.version": record_change(lambda r: r["skill"].update(version="9.9.9")),
            "skill.name": record_change(lambda r: r["skill"].update(name="another-skill")),
            "prompt.template_sha256": record_change(lambda r: r["prompt"].update(template_sha256="0" * 64)),
            "the rendered prompt": tamper_file(f"inputs/{unit}/prompt.md"),
            "a schema problem": record_change(lambda r: r.pop("agent")),
            "a decision superseding itself": record_change(lambda r: r.update(supersedes=r["decision_id"])),
            "not JSON": lambda directory: (directory / f"{unit}.record.json").write_text("{", encoding="utf-8"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                directory = self.make()
                change(directory)
                report = self.validate(directory)
                self.assertTrue(self.found(report, "T-RECORD", "error", unit), (label, [f.as_dict() for f in report.findings]))
                self.assertEqual(report.unit_verdict(unit), "error")

    def test_the_payload_and_the_record_name_the_same_skill_version(self) -> None:
        docs = te.to_v2(te.build_good(self.ACCESSION), skill_version="1.0.0")
        report = self.validate(self.make(docs=docs))
        self.assertTrue(any("written by skill version 1.0.0" in m for m in self.found(report, "T-RECORD", "error")))

    def test_the_skill_is_read_at_its_commit(self) -> None:
        # Without --code-root the skill and the template are read from git at repo_commit; a commit this repository
        # does not hold leaves them unchecked, which is a hold, never a pass.
        directory = self.make()
        report = self.validate(directory, code_root=None)
        self.assertEqual(report.verdict, "hold")
        self.assertTrue(any("cannot be read at repo_commit" in m for m in self.found(report, "T-RECORD", "hold")))

    def test_the_skill_at_head_is_read_through_git(self) -> None:
        head = records.Code.from_head()
        directory = self.make()
        bound = {"role": "archive_listing", "path": "inputs/archive-members.tsv"}
        for unit in te.build_good(self.ACCESSION):
            records.write_record(directory / f"{unit}.json", head, inputs=[bound])
        report = self.validate(directory, code_root=None)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        # The working tree's SKILL.md is not what is read: a record naming another sha256 is refused.
        self.rewrite_record(directory, self.UNIT, lambda r: r["skill"].update(sha256=sha(b"another skill")))
        self.assertTrue(self.found(self.validate(directory, code_root=None), "T-RECORD", "error", self.UNIT))


class ToolTests(V2Case):
    UNIT = "AN004454"

    def test_the_skill_lists_exactly_its_allowed_tools(self) -> None:
        text = (ROOT / records.SKILL_PATH).read_text(encoding="utf-8")
        self.assertEqual(vt.skill_allowed_tools(text), ALLOWED_TOOLS)
        self.assertEqual(vt.skill_version(text), "1.2.0")
        self.assertEqual(vt.skill_frontmatter(text)["name"], "msdial-unit-triage")
        for tool in ALLOWED_TOOLS:
            self.assertNotIn(tool.split("__")[-1], ("msdial_catalog_save_class_proposal", "msdial_catalog_reanalysis_handoff"))
        for name in ("Bash", "WebFetch", "Edit", "mcp__msdial-interactive__msdial_download_repository_raw",
                     "mcp__msdial-repository-catalog__msdial_catalog_save_class_proposal",
                     "mcp__msdial-repository-catalog__msdial_catalog_get_analysis_unit"):
            self.assertFalse(vt.tool_allowed(name, ALLOWED_TOOLS), name)
        self.assertTrue(vt.tool_allowed("triage_spec_check", ALLOWED_TOOLS))
        self.assertTrue(vt.tool_allowed(CHECK, ALLOWED_TOOLS))
        self.assertFalse(vt.tool_allowed("mcp__another-server__triage_spec_check", ALLOWED_TOOLS))

    def test_the_prompt_template_names_its_placeholders(self) -> None:
        text = (ROOT / records.TEMPLATE_PATH).read_text(encoding="utf-8")
        names = ("accession", "unit_ids", "input_dir", "output_dir", "analysis_purpose", "spec_name")
        for name in names:
            self.assertIn("{{" + name + "}}", text)
        self.assertNotIn("{{handoff_sha256}}", text, "code writes the handoff hash; the agent no longer copies it")
        self.assertNotRegex(records.render(text.encode("utf-8"), accession="ST000001", unit_ids="AN000001", input_dir="in",
                                           output_dir="out", analysis_purpose="null", spec_name="ST000001.spec.json").decode("utf-8"),
                            r"\{\{[a-z0-9_]+\}\}")
        revision = (ROOT / records.REVISION_TEMPLATE_PATH).read_text(encoding="utf-8")
        for name in (*names, "revisions", "previous_spec"):
            self.assertIn("{{" + name + "}}", revision)

    def test_allowed_tool_calls_pass_and_others_are_refused(self) -> None:
        allowed = [(CHECK, {"spec_file": "../out/ST002747.spec.json"}, {"ok": True}),
                   ("triage_spec_check", {"spec_file": "../out/ST002747.spec.json"}, {"ok": True}),
                   ("Read", {"file_path": "inputs/archive-members.tsv"}, {"lines": 3}),
                   ("Write", {"file_path": f"{self.UNIT}.json"}, {"ok": True})]
        report = self.validate(self.make(tool_calls=allowed))
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        for tool in ("Bash", "WebFetch", "mcp__msdial-interactive__msdial_list_remote_archive",
                     "mcp__msdial-repository-catalog__msdial_catalog_save_class_proposal",
                     "mcp__msdial-repository-catalog__msdial_catalog_get_analysis_unit"):
            with self.subTest(tool):
                report = self.validate(self.make(tool_calls=[*allowed, (tool, {}, {"ok": True})]))
                self.assertTrue(any(tool in m for m in self.found(report, "T-TOOLS", "error")), tool)

    def test_tool_results_and_their_order_are_checked(self) -> None:
        calls = [("Read", {"file_path": "inputs/archive-members.tsv"}, {"lines": 3}),
                 ("triage_spec_check", {"spec_file": "../out/ST002747.spec.json"}, {"ok": True})]
        directory = self.make(tool_calls=calls)
        result = directory / "inputs" / self.UNIT / "calls" / "2.json"
        result.write_bytes(result.read_bytes() + b" ")
        self.assertTrue(self.found(self.validate(directory), "T-TOOLS", "error", self.UNIT))
        directory = self.make(tool_calls=calls)
        self.rewrite_record(directory, self.UNIT, lambda r: r["tool_calls"].reverse())
        self.assertTrue(any("has seq" in m for m in self.found(self.validate(directory), "T-TOOLS", "error", self.UNIT)))

    CALLS = [(CHECK, {"spec_file": "../out/ST002747.spec.json"}, {"ok": True}),
             ("Write", {"file_path": "AN004454.json"}, "File created")]

    def _release(self, directory: Path, data: bytes) -> None:
        """Replace the unit's released transcript with `data`, the record naming its sha256."""
        path = directory / "transcripts" / f"{self.UNIT}.redacted.jsonl"
        path.write_bytes(data)
        self.rewrite_record(directory, self.UNIT, lambda r: r["transcript"].update(sha256=sha(data)))

    def test_the_released_transcript_is_hashed_and_its_tool_calls_compared(self) -> None:
        directory = self.make(tool_calls=self.CALLS)
        report = self.validate(directory)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        data = (directory / "transcripts" / f"{self.UNIT}.redacted.jsonl").read_bytes()
        self.assertEqual(vt.transcript_tool_names(data), [c[0] for c in self.CALLS])
        # A harness may repeat a streamed message; one id is one call.
        lines = data.decode("utf-8").splitlines()
        self._release(directory, ("\n".join([*lines[:2], lines[1], *lines[2:]]) + "\n").encode("utf-8"))
        self.assertEqual(self.validate(directory).verdict, "pass")
        # Another sha256 is refused.
        self.rewrite_record(directory, self.UNIT, lambda r: r["transcript"].update(sha256="0" * 64))
        self.assertTrue(self.found(self.validate(directory), "T-RECORD", "error", self.UNIT))
        # A call the record leaves out, and a tool the skill does not allow, are refused.
        directory = self.make(tool_calls=self.CALLS)
        self._release(directory, records.transcript_lines([*self.CALLS, ("Bash", {}, "refused")]))
        errors = self.found(self.validate(directory), "T-TOOLS", "error", self.UNIT)
        self.assertTrue(any("differ from" in m for m in errors), errors)
        self.assertTrue(any("shows a call to Bash" in m for m in errors), errors)

    def test_arguments_results_server_model_and_session_are_compared(self) -> None:
        other_server = [("mcp__another-server__triage_spec_check", *self.CALLS[0][1:]), self.CALLS[1]]
        cases = {
            "arguments": (records.transcript_lines([(self.CALLS[0][0], {"unit_id": "AN004455"}, {"ok": True}),
                                                    self.CALLS[1]]), "T-TOOLS", "arguments"),
            "result": (records.transcript_lines([(self.CALLS[0][0], self.CALLS[0][1], {"ok": False}), self.CALLS[1]]),
                       "T-TOOLS", "not the result"),
            "server": (records.transcript_lines(other_server), "T-TOOLS", "another-server"),
            "model": (records.transcript_lines(self.CALLS, model="another-model"), "T-RECORD", "model_id"),
            "session": (records.transcript_lines(self.CALLS, session="another-session"), "T-RECORD", "session_id"),
        }
        for label, (data, check, words) in cases.items():
            with self.subTest(label):
                directory = self.make(tool_calls=self.CALLS)
                self._release(directory, data)
                errors = self.found(self.validate(directory), check, "error", self.UNIT)
                self.assertTrue(any(words in m for m in errors), (label, errors))

    def test_a_record_without_a_released_transcript_holds(self) -> None:
        # Fail closed: without the harness's own account, tool_calls and the context are only the wrapper's word.
        for transcript in (None, {"sha256": None, "released_path": None, "unredacted_sha256": "a" * 64, "redaction": None}):
            with self.subTest(transcript=transcript):
                report = self.validate(self.make(tool_calls=self.CALLS, transcript=transcript))
                self.assertTrue(self.found(report, "T-TRANSCRIPT", "hold", self.UNIT))
                self.assertEqual(report.unit_verdict(self.UNIT), "hold")
        directory = self.make(tool_calls=self.CALLS)
        self.rewrite_record(directory, self.UNIT, lambda r: r["transcript"].update(released_path="transcripts/elsewhere.jsonl"))
        report = self.validate(directory)
        self.assertTrue(self.found(report, "T-TRANSCRIPT", "hold", self.UNIT))
        directory = self.make(tool_calls=self.CALLS)
        self.rewrite_record(directory, self.UNIT, lambda r: r["agent"].update(model_id="unknown"))
        self.assertTrue(self.found(self.validate(directory), "T-RECORD", "hold", self.UNIT))

    def test_a_call_without_a_stored_result_holds(self) -> None:
        directory = self.make(tool_calls=self.CALLS)
        self.rewrite_record(directory, self.UNIT, lambda r: r["tool_calls"][0].update(
            status="error", result_path=None, result_sha256=None))
        report = self.validate(directory)
        self.assertTrue(any("no stored result" in m for m in self.found(report, "T-TOOLS", "hold", self.UNIT)))
        self.assertNotEqual(report.unit_verdict(self.UNIT), "pass")


class EvidenceTests(V2Case):
    UNIT = "AN004454"

    def test_a_record_binding_no_evidence_holds(self) -> None:
        directory = self.make()
        self.rewrite_record(directory, self.UNIT, lambda r: r.update(inputs=[]))
        messages = self.found(self.validate(directory), "T-RECORD", "hold", self.UNIT)
        self.assertTrue(any("no Catalog evidence" in m for m in messages), messages)
        self.assertTrue(any("no archive listing" in m for m in messages), messages)

    def test_the_payload_handoff_hash_is_the_bound_copy(self) -> None:
        directory = self.make()
        handoff = directory / "inputs" / self.UNIT / "handoff.json"
        handoff.write_text("{}", encoding="utf-8")
        payload = json.loads((directory / f"{self.UNIT}.json").read_text(encoding="utf-8"))
        bound = {"role": "catalog_handoff", "path": f"inputs/{self.UNIT}/handoff.json"}
        listing = {"role": "archive_listing", "path": "inputs/archive-members.tsv"}
        records.write_record(directory / f"{self.UNIT}.json", self.code, inputs=[bound, listing])
        self.assertNotEqual(payload["catalog_handoff_sha256"], sha(b"{}"))
        self.assertTrue(any("handoff its record binds" in m
                            for m in self.found(self.validate(directory), "T-HANDOFF", "error", self.UNIT)))

    def test_the_listings_given_are_the_ones_the_records_bind(self) -> None:
        directory = self.make()
        bound = directory / "inputs" / "archive-members.tsv"
        self.assertEqual(self.validate(directory, listing_files=[bound]).verdict, "pass")
        other = self.tmp / "other-listing.tsv"
        other.write_bytes(bound.read_bytes() + b"\n")
        report = self.validate(directory, listing_files=[other])
        self.assertTrue(any("not among the --archive-listing files" in m for m in self.found(report, "T-RECORD", "hold")))
        self.assertTrue(any("no decision record binds it" in m for m in self.found(report, "T-DUP", "hold")))

    def test_the_mcp_servers_and_flags_are_stated(self) -> None:
        directory = self.make(tool_calls=ToolTests.CALLS)
        self.rewrite_record(directory, self.UNIT, lambda r: r["agent"]["context"].update(mcp_servers=[], harness_flags=[]))
        messages = self.found(self.validate(directory), "T-CONTEXT", "hold", self.UNIT)
        self.assertTrue(any("does not list" in m for m in messages), messages)
        self.assertTrue(any("harness_flags is empty" in m for m in messages), messages)
        directory = self.make()
        self.rewrite_record(directory, self.UNIT, lambda r: r["agent"].update(harness="claude-code"))
        messages = self.found(self.validate(directory), "T-CONTEXT", "hold", self.UNIT)
        self.assertTrue(any("--strict-mcp-config" in m for m in messages), messages)
        flags = [*vt.CLAUDE_CODE_CLEAN_FLAGS, "--model", "x"]
        self.rewrite_record(directory, self.UNIT, lambda r: r["agent"]["context"].update(harness_flags=flags))
        self.assertFalse(self.found(self.validate(directory), "T-CONTEXT", unit=self.UNIT))

    def test_the_wrapper_is_identified_at_its_commit(self) -> None:
        directory = self.make()
        self.rewrite_record(directory, self.UNIT, lambda r: r["wrapper"].update(repo_commit=None))
        self.assertTrue(any("not a commit's" in m for m in self.found(self.validate(directory), "T-RECORD", "hold", self.UNIT)))
        self.rewrite_record(directory, self.UNIT, lambda r: r["wrapper"].update(repo_commit="c" * 40, sha256="0" * 64))
        self.assertTrue(self.found(self.validate(directory), "T-RECORD", "error", self.UNIT))

    def test_a_released_code_folder_with_crlf_line_endings_is_read_as_its_commit(self) -> None:
        directory = self.make()
        for relative in (records.SKILL_PATH, records.TEMPLATE_PATH, records.WRAPPER_PATH):
            path = self.code.root / relative
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        report = self.validate(directory)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])


class ContextTests(V2Case):
    UNIT = "AN004454"

    def test_a_context_that_is_not_clean_holds(self) -> None:
        clean = records.CLEAN_CONTEXT
        for label, context in (
                ("configuration", {**clean, "configuration": "user_settings"}),
                ("a context file", {**clean, "context_files": [{"path": "CLAUDE.md", "sha256": "a" * 64}]}),
                ("memory", {**clean, "memory": "enabled"}),
                ("a hook", {**clean, "hooks": ["PreToolUse"]}),
                ("another MCP server", {**clean, "mcp_servers": [*clean["mcp_servers"],
                                                                  {"name": "msdial-interactive", "version": "0.5.69"}]})):
            with self.subTest(label):
                directory = self.make()
                self.rewrite_record(directory, self.UNIT, lambda r: r["agent"].update(context=context))
                report = self.validate(directory)
                self.assertTrue(self.found(report, "T-CONTEXT", "hold", self.UNIT), label)
                self.assertEqual(report.unit_verdict(self.UNIT), "hold")

    def test_a_context_file_is_hashed_when_it_is_present(self) -> None:
        directory = self.make()
        (directory / "inputs" / "CLAUDE.md").write_text("a context file", encoding="utf-8")
        context = {**records.CLEAN_CONTEXT, "context_files": [{"path": "inputs/CLAUDE.md", "sha256": "a" * 64}]}
        self.rewrite_record(directory, self.UNIT, lambda r: r["agent"].update(context=context))
        report = self.validate(directory)
        self.assertTrue(self.found(report, "T-RECORD", "error", self.UNIT))


class ReviewLogTests(V2Case):
    UNIT = "AN004454"

    def flagged(self, *, code: str = "mixed_acquisition", status: str = "ready") -> dict:
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs[self.UNIT]["flags"].append({"code": code, "certainty": "suspected", "detail": "DDA and DIA files in one unit suspected."})
        docs[self.UNIT]["status"] = status
        if status == "needs_person":
            docs[self.UNIT]["needs_person"].append({"code": "mixed_acquisition", "question": "Do the acquisitions form one run?"})
        return docs

    def answer(self, directory: Path, decision_id: str, code: str, by: str = "Synthetic Reviewer") -> dict:
        return curated.append_review_log(directory, "person_answer", decision_id, AT, by=by, question_code=code,
                                         answer="One run; the DIA files are left to a later unit.",
                                         channel="conversation")

    def test_a_ready_unit_with_a_blocking_flag_needs_a_logged_answer(self) -> None:
        directory = self.make(docs=self.flagged())
        report = self.validate(directory)
        self.assertTrue(any("review-log.jsonl" in m for m in self.found(report, "T-STATUS", "error", self.UNIT)))
        payload = sha((directory / f"{self.UNIT}.json").read_bytes())
        # An answer to another code, or about another decision, does not clear it.
        self.answer(directory, payload, "ms1_only")
        self.answer(directory, "f" * 64, "mixed_acquisition")
        self.assertTrue(self.found(self.validate(directory), "T-STATUS", "error", self.UNIT))
        self.answer(directory, payload, "mixed_acquisition")
        report = self.validate(directory)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])

    def test_an_answer_about_a_superseded_payload_counts(self) -> None:
        directory = self.make(docs=self.flagged(status="needs_person"))
        base = sha((directory / f"{self.UNIT}.json").read_bytes())
        self.answer(directory, base, "mixed_acquisition")
        revised = self.flagged()  # ready, the flag kept
        path = directory / f"{self.UNIT}.r1.json"
        path.write_text(json.dumps(revised[self.UNIT], indent=1), encoding="utf-8")
        records.write_record(path, self.code, inputs=[{"role": "archive_listing", "path": "inputs/archive-members.tsv"}],
                             supersedes=base)
        report = self.validate(directory)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        # Without the record's supersedes the answer is about another decision.
        records.write_record(path, self.code, inputs=[{"role": "archive_listing", "path": "inputs/archive-members.tsv"}])
        self.assertTrue(self.found(self.validate(directory), "T-STATUS", "error", self.UNIT))

    def test_a_revision_drops_a_blocking_flag_only_on_a_cited_answer(self) -> None:
        directory = self.make(docs=self.flagged(status="needs_person"))
        base = sha((directory / f"{self.UNIT}.json").read_bytes())
        clean = te.to_v2(te.build_good(self.ACCESSION))[self.UNIT]
        path = directory / f"{self.UNIT}.r1.json"
        path.write_text(json.dumps(clean, indent=1), encoding="utf-8")
        listing = [{"role": "archive_listing", "path": "inputs/archive-members.tsv"}]
        records.write_record(path, self.code, inputs=listing, supersedes=base)
        revision = sha(path.read_bytes())
        report = self.validate(directory)
        self.assertTrue(any("mixed_acquisition" in m for m in self.found(report, "T-REVISION", "error", self.UNIT)))
        # A revision entry whose cause is a validator verdict, or an answer to another code, does not carry it.
        verdict = curated.append_review_log(directory, "validator_verdict", base, AT, tool="validate-triage.py",
                                            code_commit=None, verdict="hold", findings_sha256="0" * 64, findings=[])
        other = self.answer(directory, base, "ms1_only")
        for cause in (verdict, other):
            curated.append_review_log(directory, "revision", base, AT, supersedes=base, new_decision_id=revision,
                                      cause={"kind": cause["kind"], "seq": cause["seq"]})
        self.assertTrue(self.found(self.validate(directory), "T-REVISION", "error", self.UNIT))
        answer = self.answer(directory, base, "mixed_acquisition")
        curated.append_review_log(directory, "revision", base, AT, supersedes=base, new_decision_id=revision,
                                  cause={"kind": "person_answer", "seq": answer["seq"]})
        report = self.validate(directory)
        self.assertFalse(self.found(report, "T-REVISION"), [f.as_dict() for f in report.findings])
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])

    def test_a_revision_without_a_record_is_compared_with_the_one_before_it(self) -> None:
        directory = self.make(docs=self.flagged(status="needs_person"))
        clean = te.to_v2(te.build_good(self.ACCESSION))[self.UNIT]
        (directory / f"{self.UNIT}.r1.json").write_text(json.dumps(clean, indent=1), encoding="utf-8")
        report = self.validate(directory)
        self.assertTrue(self.found(report, "T-REVISION", "error", self.UNIT))
        self.assertTrue(self.found(report, "T-RECORD", "hold", self.UNIT))

    def test_a_broken_review_log_is_an_error(self) -> None:
        directory = self.make()
        self.answer(directory, "a" * 64, "ms1_only")
        self.answer(directory, "a" * 64, "ms1_only")
        log = directory / vt.REVIEW_LOG
        lines = log.read_bytes().split(b"\n")
        lines[0] = lines[0].replace(b"One run", b"Two runs")
        log.write_bytes(b"\n".join(lines))
        report = self.validate(directory)
        self.assertTrue(self.found(report, "T-LOG", "error"))
        self.assertEqual(report.verdict, "error")

    def test_v1_answers_still_come_from_the_payload(self) -> None:
        docs = te.build_good(self.ACCESSION)
        docs[self.UNIT]["flags"].append({"code": "mixed_acquisition", "certainty": "suspected", "detail": "DDA and DIA suspected."})
        directory = te.write(docs, self.tmp / "v1-answers")
        self.assertTrue(self.found(self.validate(directory), "T-STATUS", "error", self.UNIT))
        docs[self.UNIT]["person_answers"] = [{"by": "Synthetic Reviewer", "answered_at": AT, "code": "mixed_acquisition",
                                             "answer": "One run."}]
        te.write(docs, directory)
        self.assertEqual(self.validate(directory).verdict, "pass")


class ClassBoundaryTests(V2Case):
    def test_a_v2_triage_citing_the_class_is_refused(self) -> None:
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs["AN004454"]["rows"][0]["evidence"].append({"source": "catalog_unit", "field": "class_proposal.assignments",
                                                        "value": "Treated"})
        report = self.validate(self.make(docs=docs))
        self.assertTrue(self.found(report, "T-CLASS", "error", "AN004454"))


class InScopeDecisionTests(V2Case):
    """The decisions of 2026-11-18. D1: inclusion- or exclusion-list DDA is untargeted DDA, in scope; targeted_dda is an
    informational flag, never a question. D2: shared_content is for content a unit keeps; byte-identical copies among
    excluded members hold nothing."""

    UNIT = "AN004454"

    def test_a_ready_unit_flagged_targeted_dda_passes_without_an_answer(self) -> None:
        self.assertNotIn("targeted_dda", vt.BLOCKING_FLAGS)
        self.assertIn("targeted_dda", vt.INFORMATIONAL_FLAGS)
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs[self.UNIT]["flags"].append({"code": "targeted_dda", "certainty": "suspected",
                                         "detail": "Inclusion-list DDA: MS2 selection follows the list."})
        report = self.validate(self.make(docs=docs))
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])

    def test_a_scope_targeted_dda_question_is_a_schema_error(self) -> None:
        self.assertNotIn("scope_targeted_dda", vt.load_schema(vt.SCHEMA_V2_ID)["$defs"]["question_code"]["enum"])
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs[self.UNIT]["status"] = "needs_person"
        docs[self.UNIT]["needs_person"].append({"code": "scope_targeted_dda", "question": "Is the inclusion list in scope?"})
        self.assertTrue(self.found(self.validate(self.make(docs=docs)), "T-SCHEMA", "error", self.UNIT))

    def test_dda_beside_targeted_dda_is_not_a_mix(self) -> None:
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs[self.UNIT]["rows"][0]["acquisition_mode"] = "targeted_DDA"
        report = self.validate(self.make(docs=docs))
        self.assertTrue(any("targeted_dda flag" in m for m in self.found(report, "T-ACQ", "error", self.UNIT)),
                        "targeted_DDA kept without the flag stays an error (the reporting guarantee)")
        self.assertFalse(any("mix acquisition" in m for m in self.found(report, "T-ACQ", "error", self.UNIT)))
        docs[self.UNIT]["flags"].append({"code": "targeted_dda", "certainty": "suspected", "detail": "Inclusion list."})
        report = self.validate(self.make(docs=docs))
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])

    def test_copies_among_excluded_members_need_no_flag(self) -> None:
        # ST002747's mzXML copies have byte-identical twins, all excluded: nobody keeps the content (K empty).
        twins = vt.content_twins(vt.read_listing(te.load(self.ACCESSION)[2]))
        self.assertTrue(twins)
        report = self.validate(self.make())
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        self.assertFalse(self.found(report, "T-DUP"))
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs[self.UNIT]["flags"].append({"code": "duplicate_copies_excluded", "certainty": "confirmed",
                                         "detail": "The mzXML copies are byte-identical; all are excluded."})
        self.assertEqual(self.validate(self.make(docs=docs)).verdict, "pass")

    def test_shared_content_with_no_kept_twin_is_an_error(self) -> None:
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs[self.UNIT]["status"] = "needs_person"
        docs[self.UNIT]["flags"].append({"code": "shared_content", "certainty": "confirmed",
                                         "detail": "The mzXML copies are byte-identical."})
        docs[self.UNIT]["needs_person"].append({"code": "shared_content", "question": "Which copy?"})
        found = self.found(self.validate(self.make(docs=docs)), "T-DUP", "error", self.UNIT)
        self.assertTrue(any("duplicate_copies_excluded" in m for m in found), found)

    def test_one_or_more_keepers_of_shared_content_still_wait_for_a_person(self) -> None:
        # K = 1: one unit keeps a copy whose twins are excluded elsewhere (the failure shape where a folder name chose
        # the copy). K >= 2: two units keep identical content. Both stay blocking.
        report = self.validate(self.make("ST002283"), "ST002283")
        self.assertTrue(self.found(report, "T-DUP", "hold", "AN003731"))
        self.assertFalse(self.found(report, "T-DUP", "error"))
        docs = te.to_v2(te.build_good("ST002283"))
        docs["AN003731"]["flags"] = []
        docs["AN003731"]["needs_person"] = []
        docs["AN003731"]["status"] = "ready"
        self.assertTrue(self.found(self.validate(self.make("ST002283", docs=docs), "ST002283"), "T-DUP", "error", "AN003731"))


class SpecDerivationTests(V2Case):
    """T-SPEC and T-DIGEST: a payload expanded from the agent's spec is that spec's deterministic image over the inputs
    its record binds, by the expander at its commit; the digest is the digest script's image of the same inputs."""

    def spec_dir(self, accession: str | None = None, code: records.Code | None = None) -> Path:
        self._n += 1
        accession = accession or self.ACCESSION
        return records.spec_triage(self.tmp / f"spec-{accession}-{self._n}", accession, code or self.code)

    def validate_dir(self, directory: Path, **options) -> "vt.Report":
        inputs = directory / "inputs"
        options.setdefault("code_root", self.code.root)
        return vt.validate(directory, vt.read_catalog(inputs), [m for t in sorted(inputs.glob("*.tsv")) for m in vt.read_listing(t)],
                           listing_files=sorted(inputs.glob("*.tsv")), **options)

    def test_an_expanded_triage_passes_and_its_records_are_small(self) -> None:
        directory = self.spec_dir()
        report = self.validate_dir(directory)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        self.assertEqual([p.name for p in vt.current_triage_files(directory)],
                         ["AN004454.json", "AN004455.json", "AN004456.json", "AN004457.json"], "the spec is never a payload")
        for record in directory.glob("*.record.json"):
            self.assertLess(record.stat().st_size, 100_000)
        report = self.validate_dir(self.spec_dir("ST002283"))
        self.assertFalse([f.as_dict() for f in report.findings if f.check in ("T-SPEC", "T-DIGEST")])

    def test_a_one_byte_payload_edit_is_refused(self) -> None:
        directory = self.spec_dir()
        path = directory / "AN004454.json"
        data = path.read_bytes().replace(b'"confidence": "medium"', b'"confidence": "high"', 1)
        path.write_bytes(data)
        self.rewrite_record(directory, "AN004454", lambda r: (r.update(decision_id=sha(data)), r["payload"].update(sha256=sha(data))))
        found = self.found(self.validate_dir(directory), "T-SPEC", "error", "AN004454")
        self.assertTrue(any("not the spec's expansion" in m for m in found), found)

    def test_a_spec_that_is_not_the_one_bound_is_refused(self) -> None:
        directory = self.spec_dir()
        spec = directory / f"{self.ACCESSION}.spec.json"
        spec.write_bytes(spec.read_bytes() + b" ")
        self.assertTrue(self.found(self.validate_dir(directory), "T-SPEC", "error", "AN004454"))

    def test_the_context_is_the_records(self) -> None:
        directory = self.spec_dir()
        self.rewrite_record(directory, "AN004455", lambda r: r["derivation"]["context"].update(model="another-model"))
        self.assertTrue(any("derivation.context.model" in m for m in self.found(self.validate_dir(directory), "T-SPEC", "error", "AN004455")))

    def test_a_derived_payload_needs_a_derivation_in_its_record(self) -> None:
        directory = self.spec_dir()
        self.rewrite_record(directory, "AN004456", lambda r: r.pop("derivation"))
        self.assertTrue(any("binds no derivation" in m for m in self.found(self.validate_dir(directory), "T-SPEC", "error", "AN004456")))

    def test_the_expander_is_read_at_its_commit_through_the_code_root(self) -> None:
        # Another commit's expander: a comment line differs, the expansion does not. It is loaded from that code, not
        # from this tree, and the payloads pass; against this tree's expander the record's sha256 does not hold.
        other = records.Code.from_root(self.tmp / "other-code")
        expander = other.root / records.EXPANDER_PATH
        expander.write_bytes(expander.read_bytes().replace(b'EXPANDER_VERSION = "1.0.0"',
                                                           b'# another commit\nEXPANDER_VERSION = "1.0.0"', 1))
        other.files[records.EXPANDER_PATH] = expander.read_bytes()
        directory = self.spec_dir(code=other)
        report = self.validate_dir(directory, code_root=other.root)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        report = self.validate_dir(directory, code_root=self.code.root)
        self.assertTrue(any("cannot be re-derived" in m for m in self.found(report, "T-SPEC", "hold", "AN004454")))
        # An expander at the record's commit that writes other bytes than the payload holds is refused.
        changed = expander.read_bytes().replace(b'f"Kept by {owner} (', b'f"Held by {owner} (', 1)
        self.assertNotEqual(changed, expander.read_bytes())
        expander.write_bytes(changed)
        self.rewrite_records(directory, lambda r: r["derivation"]["expander"].update(sha256=sha(changed)))
        report = self.validate_dir(directory, code_root=other.root)
        self.assertTrue(any("not the spec's expansion" in m for m in self.found(report, "T-SPEC", "error")))

    def test_the_digest_is_rebuilt_from_the_bound_inputs(self) -> None:
        directory = self.spec_dir()
        digest = directory / "inputs" / "digest.md"
        data = digest.read_bytes().replace(b"Source facts", b"Source facts and hints", 1)
        digest.write_bytes(data)

        def rebind(record: dict) -> None:
            record["derivation"]["digest"]["sha256"] = sha(data)
            for item in record["inputs"]:
                if item["role"] == "triage_digest":
                    item.update(sha256=sha(data), bytes=len(data))
        self.rewrite_records(directory, rebind)
        found = self.found(self.validate_dir(directory), "T-DIGEST", "error")
        self.assertTrue(any("is not what the digest script" in m for m in found), found)

    def rewrite_records(self, directory: Path, change) -> None:
        for path in sorted(directory.glob("*.record.json")):
            self.rewrite_record(directory, path.name[: -len(".record.json")], change)


SYNTHETIC_ACCESSION = "ST000001"
SYNTHETIC_UNIT = "AN000001"


def synthetic_campaign(root: Path, code: records.Code) -> tuple[Path, Path]:
    """<root>/campaign/triage/ST000001/: one ready v2 unit whose record binds the Catalog handoff (holding absolute sidecar
    paths, as the Catalog writes them) and the archive listing. Returns (campaign dir, accession dir)."""
    campaign = root / "campaign"
    folder = campaign / "triage" / SYNTHETIC_ACCESSION
    (folder / "inputs" / SYNTHETIC_UNIT).mkdir(parents=True)
    rows = [{"sample_id": "S01", "raw_file": "S01_pos.mzML"}, {"sample_id": "S02", "raw_file": "S02_pos.mzML"}]
    handoff = {"accession": SYNTHETIC_ACCESSION, "analysis_unit_id": SYNTHETIC_UNIT, "sample_metadata": rows,
               "technical_settings": {"chromatography": "RP", "ion_mode": "Positive", "acquisition_mode": "DDA"},
               "file_manifest_path": "D:\\catalog\\handoffs\\AN000001\\files.json"}
    handoff_bytes = json.dumps(handoff, indent=1).encode("utf-8")
    (folder / "inputs" / SYNTHETIC_UNIT / "handoff.json").write_bytes(handoff_bytes)
    listing = "archive\tpath\tsize\tcrc32\n" + "".join(
        f"ST000001_rawdata.zip\tRP_POS/{row['raw_file']}\t{2000 + i}\t0A0B0C0{i + 1}\n" for i, row in enumerate(rows))
    (folder / "inputs" / "ST000001_rawdata.members.tsv").write_text(listing, encoding="utf-8", newline="\n")
    kept = []
    for i, row in enumerate(rows):
        kept.append({**row, "row_index": i, "member_path": f"RP_POS/{row['raw_file']}", "archive": "ST000001_rawdata.zip",
                     "role": "sample", "ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA",
                     "basis": "declared", "confidence": "high", "rationale": "The unit declares RP positive DDA.",
                     "evidence": [{"source": "catalog_unit", "field": "technical_settings", "value": "RP Positive DDA"}]})
    payload = {
        "schema": "msdial-unit-triage.v2", "repository": "metabolomics_workbench", "accession": SYNTHETIC_ACCESSION,
        "unit_id": SYNTHETIC_UNIT, "source_subrecord_id": SYNTHETIC_UNIT, "catalog_handoff_sha256": sha(handoff_bytes),
        "catalog_row_count": 2, "standing": "model_inference", "phase": "pre_download",
        "decided_by": {"agent": "synthetic-agent", "model": "synthetic-model", "skill": "msdial-unit-triage",
                       "skill_version": code.skill_version},
        "decided_at": "2026-11-16T09:10:00+09:00", "purpose": None,
        "unit_technical": {"ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA",
                           "declared": {"chromatography": "RP", "ion_mode": "Positive", "acquisition_mode": "DDA"}},
        "status": "ready", "confidence": "high", "rationale": "One arm, both rows declared and listed.",
        "rows": kept, "added_members": [], "excluded_rows": [], "excluded_members": [], "shared_with": [],
        "needs_person": [], "flags": [],
    }
    path = folder / f"{SYNTHETIC_UNIT}.json"
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    records.write_record(path, code, inputs=[
        {"role": "catalog_handoff", "path": f"inputs/{SYNTHETIC_UNIT}/handoff.json",
         "source": "msdial_catalog_reanalysis_handoff", "catalog_version": "0.6.1", "retrieved_at": "2026-11-16T09:00:00+09:00"},
        {"role": "archive_listing", "path": "inputs/ST000001_rawdata.members.tsv",
         "retrieval": {"method": "remote_central_directory", "tool": "msdial_list_remote_archive",
                       "url": "https://www.metabolomicsworkbench.org/studydownload/ST000001_rawdata.zip",
                       "archive_bytes": 123456, "etag": None, "last_modified": None, "range_bytes_read": 4096}}])
    return campaign, folder


class ReleaseBundleTests(V2Case):
    def setUp(self) -> None:
        super().setUp()
        self.campaign, self.folder = synthetic_campaign(self.tmp, self.code)

    def release(self) -> Path:
        manifest = {"schema": "synthetic-manifest", "triage": {"directory": str(self.campaign / "triage")}}
        (self.campaign / "campaign-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        bundle = self.tmp / "bundle"
        curated.write_release(campaign_dir=self.campaign, out=bundle, workspace_root=self.tmp / "analysis",
                              approved_digest="sha256:" + "0" * 64)
        return bundle

    def check(self, folder: Path, bundle: "vt.ReleaseBundle | None" = None) -> "vt.Report":
        return vt.validate(folder, vt.read_catalog(folder / "inputs", bundle), vt.read_listing(
            folder / "inputs" / "ST000001_rawdata.members.tsv"), code_root=self.code.root, bundle=bundle)

    def test_the_campaign_directory_passes(self) -> None:
        report = self.check(self.folder)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])

    def test_a_release_bundle_is_checked_through_its_index(self) -> None:
        bundle = self.release()
        released = bundle / "triage" / SYNTHETIC_ACCESSION
        projected = released / "inputs" / SYNTHETIC_UNIT / "handoff.json"
        index = json.loads((bundle / "release-index.json").read_text(encoding="utf-8"))
        self.assertEqual([item["name"] for item in index["triage_inputs"]],
                         [f"triage/{SYNTHETIC_ACCESSION}/inputs/{SYNTHETIC_UNIT}/handoff.json"])
        self.assertNotEqual(sha(projected.read_bytes()), index["triage_inputs"][0]["original_sha256"])
        found = vt.ReleaseBundle.find(released)
        self.assertIsNotNone(found)
        report = self.check(released, found)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        # The bundle is found on its own, by the command line too.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = vt.main([str(released), "--catalog", str(released / "inputs"), "--archive-listing",
                            str(released / "inputs" / "ST000001_rawdata.members.tsv"), "--code-root", str(self.code.root),
                            "--json"])
        self.assertEqual(code, 0, out.getvalue())
        # A projection whose bytes are not the ones the index names is refused, in the record and as the Catalog.
        projected.write_bytes(projected.read_bytes().replace(b"Positive", b"Negative"))
        report = self.check(released, vt.ReleaseBundle.find(released))
        self.assertTrue(self.found(report, "T-RECORD", "error", SYNTHETIC_UNIT))
        self.assertTrue(self.found(report, "T-HANDOFF", "error", SYNTHETIC_UNIT))

    def test_an_index_naming_another_original_is_refused(self) -> None:
        bundle = self.release()
        index_path = bundle / "release-index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["triage_inputs"][0]["original_sha256"] = "0" * 64
        index_path.write_text(json.dumps(index), encoding="utf-8")
        released = bundle / "triage" / SYNTHETIC_ACCESSION
        report = self.check(released, vt.ReleaseBundle.find(released))
        self.assertTrue(any("original_sha256" in m for m in self.found(report, "T-RECORD", "error", SYNTHETIC_UNIT)))
        self.assertTrue(self.found(report, "T-HANDOFF", "error", SYNTHETIC_UNIT))

    def test_emit_selection_never_writes_into_a_bundle(self) -> None:
        bundle = self.release()
        released = bundle / "triage" / SYNTHETIC_ACCESSION
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = vt.main([str(released), "--catalog", str(released / "inputs"), "--archive-listing",
                            str(released / "inputs" / "ST000001_rawdata.members.tsv"), "--code-root", str(self.code.root),
                            "--emit-selection", "--json"])
        self.assertEqual(code, 2)
        self.assertFalse((released / vt.REVIEW_LOG).exists())


class EmitSelectionTests(V2Case):
    def run_cli(self, directory: Path, *extra: str) -> tuple[int, dict]:
        out = io.StringIO()
        d = te.fixture_dir(self.ACCESSION)
        with contextlib.redirect_stdout(out):
            code = vt.main([str(directory), "--catalog", str(d / "catalog.json"), "--archive-listing",
                            str(d / "archive-members.tsv"), "--code-root", str(self.code.root), "--json", *extra])
        return code, json.loads(out.getvalue())

    def test_the_verdict_of_each_decision_is_appended_once(self) -> None:
        directory = self.make()
        code, report = self.run_cli(directory, "--emit-selection")
        self.assertEqual(code, 0)
        log = directory / vt.REVIEW_LOG
        problems, shas = curated.verify_review_log(log)
        self.assertEqual(problems, [])
        entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([entry["kind"] for entry in entries], ["validator_verdict"] * 4)
        payloads = {unit: sha((directory / f"{unit}.json").read_bytes()) for unit in te.build_good(self.ACCESSION)}
        self.assertEqual(sorted(entry["decision_id"] for entry in entries), sorted(payloads.values()))
        for entry in entries:
            self.assertEqual(set(entry), {"seq", "at", "kind", "decision_id", "prev_sha256", "tool", "code_commit", "verdict",
                                          "findings_sha256", "findings"})
            self.assertEqual((entry["tool"], entry["verdict"]), ("validate-triage.py", "pass"))
            self.assertEqual(entry["findings_sha256"], sha(curated.canonical(entry["findings"])))
        self.assertEqual(report["review_log"]["head_sha256"], shas[-1])
        self.assertEqual(sorted(report["review_log"]["appended"]), sorted(payloads))
        # The selection is the plan's: msdial-selection-canonical.v1 over the listing the record binds.
        for unit, block in report["selection"].items():
            triage = json.loads((directory / f"{unit}.json").read_text(encoding="utf-8"))
            bound = curated.record_inputs(curated.record_path(directory / f"{unit}.json"), payloads[unit])["listing"]
            self.assertEqual(block["decision_id"], payloads[unit])
            self.assertEqual(block["selection_sha256"], curated.selection_sha256(triage, bound))
            self.assertNotEqual(block["selection_sha256"], curated.selection_sha256(triage, {}))
            self.assertEqual(block["selection_canonical_form"], "msdial-selection-canonical.v1")
            self.assertEqual(block["selection_listings"][0]["path"], "inputs/archive-members.tsv")
        # Run again on the same files: nothing new to record.
        code, report = self.run_cli(directory, "--emit-selection")
        self.assertEqual(report["review_log"]["appended"], [])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 4)
        # Without --emit-selection the script writes nothing, and its report carries no selection.
        code, report = self.run_cli(directory)
        self.assertNotIn("selection", report)
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 4)

    def test_a_hold_and_its_findings_are_recorded_per_decision(self) -> None:
        docs = te.to_v2(te.build_good(self.ACCESSION))
        docs["AN004454"]["rows"][0]["confidence"] = "low"
        directory = self.make(docs=docs)
        code, report = self.run_cli(directory, "--emit-selection")
        self.assertEqual(code, 1)
        entries = {json.loads(line)["decision_id"]: json.loads(line)
                   for line in (directory / vt.REVIEW_LOG).read_text(encoding="utf-8").splitlines()}
        held = entries[sha((directory / "AN004454.json").read_bytes())]
        self.assertEqual(held["verdict"], "hold")
        self.assertEqual([item["check"] for item in held["findings"]], ["T-CONFIDENCE"])
        self.assertEqual(entries[sha((directory / "AN004455.json").read_bytes())]["verdict"], "pass")

    def test_children_get_their_own_selection(self) -> None:
        accession = next(a for a in te.ACCESSIONS if any(d.get("children") for d in te.build_good(a).values()))
        directory = self.make(accession)
        report = self.validate(directory, accession)
        vt.emit_selection(report, directory, at=AT, commit=None)
        for unit, doc in te.to_v2(te.build_good(accession)).items():
            block = report.extra["selection"][unit]
            if not doc.get("children"):
                self.assertEqual(block["children"], {})
                continue
            triage = json.loads((directory / f"{unit}.json").read_text(encoding="utf-8"))
            bound = curated.record_inputs(curated.record_path(directory / f"{unit}.json"), block["decision_id"])["listing"]
            self.assertEqual(block["children"], {c["child_key"]: curated.selection_sha256(triage, bound, c["child_key"])
                                                 for c in doc["children"]})

    def test_a_broken_log_is_not_appended_to(self) -> None:
        directory = self.make()
        for _ in range(2):
            curated.append_review_log(directory, "person_answer", "a" * 64, AT, by="Synthetic Reviewer",
                                      question_code="ms1_only", answer="yes", channel="conversation")
        log = directory / vt.REVIEW_LOG
        log.write_bytes(log.read_bytes().replace(b'"yes"', b'"no"'))
        before = log.read_bytes()
        code, report = self.run_cli(directory, "--emit-selection")
        self.assertEqual(code, 2)
        self.assertIn("refused", report["review_log"])
        self.assertEqual(log.read_bytes(), before)


class RunValidatorContractTests(V2Case):
    """curated.run_validator (the plan's reader) runs the script as a subprocess, with the Catalog and listings the
    accession's inputs\\ hold and no --code-root: the records are checked against git at their repo_commit."""

    def accession_dir(self) -> Path:
        head = records.Code.from_head()
        folder = self.tmp / "triage" / self.ACCESSION
        te.write(te.to_v2(te.build_good(self.ACCESSION), skill_version=head.skill_version), folder)
        (folder / "inputs").mkdir(exist_ok=True)
        (folder / "inputs" / "catalog.json").write_bytes((te.fixture_dir(self.ACCESSION) / "catalog.json").read_bytes())
        bound = records.copy_listing(te.load(self.ACCESSION)[2], folder)
        for unit in te.build_good(self.ACCESSION):
            records.write_record(folder / f"{unit}.json", head, inputs=[bound])
        return folder

    def test_the_output_contract_the_plan_reads_is_unchanged(self) -> None:
        folder = self.accession_dir()
        result = curated.run_validator(folder)
        self.assertEqual((result["verdict"], result["exit_code"]), ("pass", 0), json.dumps(result.get("report"), indent=1)[:3000])
        report = result["report"]
        self.assertLessEqual({"accession", "verdict", "units", "findings"}, set(report))
        self.assertEqual(result["report_sha256"], sha(curated.canonical(report)))
        for unit in report["units"].values():
            self.assertEqual(set(unit), {"status", "verdict", "schema"})
        self.assertNotIn("selection", report)
        self.assertFalse((folder / vt.REVIEW_LOG).exists(), "the plan's validator run writes nothing")
        # A context that was not clean holds the accession, and the plan reads it as a hold.
        self.rewrite_record(folder, "AN004454", lambda r: r["agent"]["context"].update(memory="enabled"))
        result = curated.run_validator(folder)
        self.assertEqual((result["verdict"], result["exit_code"]), ("hold", 1))

    def test_one_payload_changed_does_not_change_another_units_report(self) -> None:
        # The plan records the report's sha256 per unit; it must not move when only another unit's bytes move.
        folder = self.accession_dir()
        first = curated.run_validator(folder)["report_sha256"]
        doc = json.loads((folder / "AN004455.json").read_text(encoding="utf-8"))
        doc["notes"] = "a reworded note"
        (folder / "AN004455.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        records.write_record(folder / "AN004455.json", records.Code.from_head(),
                             inputs=[{"role": "archive_listing", "path": "inputs/archive-members.tsv"}])
        self.assertEqual(curated.run_validator(folder)["report_sha256"], first)


if __name__ == "__main__":
    unittest.main()
