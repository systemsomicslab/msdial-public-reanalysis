"""scripts/run-triage-agent.py: the triage wrapper of docs/llm-assisted-curation.md section 2.3 ("Context" and the
envelope).

The harness is tests/fake_claude.py, which takes the wrapper's real flags, emits a canned stream-JSON transcript and
writes the payloads it is given; the MCP server is tests/fake_mcp_server.py; the Catalog, the remote archive listing
and the mwTab fetch are in-process fakes over the eval fixtures. Checked: the inputs and the session prepared (dry run
included), the clean-configuration flags, the envelope written from the transcript (hashes the validator recomputes,
tool calls in order with secrets, e-mail addresses, the operator's names and absolute paths removed, the results
stored), a configuration that is not clean recorded as such and held, and every refusal: a decision record or any
other file the model writes, a write outside the output folder, a failed or timed-out session, an existing payload.
Synthetic throughout: public accession ids only, no person, no real model.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "scripts"))
import decision_records as records  # noqa: E402
import triage_eval as te  # noqa: E402
from campaign import curated  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("run_triage_agent_under_test", ROOT / "scripts" / "run-triage-agent.py")
assert _SPEC and _SPEC.loader
rta = importlib.util.module_from_spec(_SPEC)
sys.modules["run_triage_agent_under_test"] = rta
_SPEC.loader.exec_module(rta)
vt = rta.validator

FAKE_CLAUDE = TESTS / "fake_claude.py"
FAKE_MCP = TESTS / "fake_mcp_server.py"
CATALOG = "msdial-repository-catalog"
SERVER_TOOLS = ["msdial_catalog_status", "msdial_catalog_search", "msdial_catalog_get_analysis_unit",
                "msdial_catalog_save_class_proposal", "msdial_catalog_reanalysis_handoff"]
ALLOWED = ["Read", "Glob", "Grep", "Write", f"mcp__{CATALOG}__msdial_catalog_status",
           f"mcp__{CATALOG}__msdial_catalog_search", f"mcp__{CATALOG}__msdial_catalog_get_analysis_unit"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeCatalog:
    """The eval fixtures' units as the Catalog would hand them off: a handoff file with its three sidecars beside a
    database folder, no Class proposal."""

    version = "0.0.0-test"

    def __init__(self, folder: Path, accessions: list[str]) -> None:
        self.folder = folder / "catalog-data" / "handoffs"
        self.folder.mkdir(parents=True)
        self.units_by_accession = {acc: te.load(acc)[0] for acc in accessions}
        self.calls: list[str] = []

    def units(self, accession: str) -> list[dict[str, Any]]:
        catalog = self.units_by_accession.get(accession) or {"units": []}
        return [{"unit_id": u["unit_id"], "repository": catalog.get("repository", "metabolomics_workbench"),
                 "source_subrecord_id": u.get("source_subrecord_id") or u["unit_id"]} for u in catalog["units"]]

    def handoff(self, unit_id: str) -> dict[str, Any]:
        self.calls.append(unit_id)
        for accession, catalog in self.units_by_accession.items():
            for unit in catalog["units"]:
                if unit["unit_id"] != unit_id:
                    continue
                archive = te.load(accession)[2].read_text(encoding="utf-8").splitlines()[1].split("\t")[0]
                url = f"https://www.metabolomicsworkbench.org/studydownload/{archive}"
                paths = {}
                for key, suffix, value in (("sample_table_path", "samples", unit["rows"]),
                                           ("file_manifest_path", "files", [{"download_url": url}]),
                                           ("analysis_input_manifest_path", "inputs", [])):
                    path = self.folder / f"{unit_id}-{suffix}.json"
                    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
                    paths[key] = str(path)
                handoff = {
                    "schema": "msdial-repository-reanalysis-handoff.v1", "repository": catalog.get("repository"),
                    "accession": accession, "analysis_unit_id": unit_id,
                    "source_subrecord_id": unit.get("source_subrecord_id") or unit_id,
                    "technical_settings": unit["declared"], "files": [{"download_url": url}],
                    "download_scope": {"bundle_urls": [{"url": url, "bytes": 1234567}], "objects": []},
                    "sample_metadata": unit["rows"], "class_proposal": None, "blocking_reasons": ["class_proposal:missing"],
                    **paths,
                }
                path = self.folder / f"{unit_id}.json"
                path.write_text(json.dumps(handoff, indent=2), encoding="utf-8")
                return {"handoff_path": str(path)}
        raise KeyError(unit_id)


def fake_lister(url: str, expected: int | None) -> dict[str, Any]:
    archive = url.rsplit("/", 1)[-1]
    for accession in sorted(os.listdir(te.FIXTURES)):
        listing = te.load(accession)[2]
        lines = listing.read_text(encoding="utf-8").splitlines()
        if lines[1].split("\t")[0] != archive:
            continue
        members = []
        for line in lines[1:]:
            _archive, path, size, crc = line.split("\t")[:4]
            members.append({"path": path, "is_dir": False, "size": int(size), "crc32": crc})
        return {"ok": True, "url": url, "size_bytes": expected, "members": members,
                "provenance": {"bytes_fetched": 17340, "etag": '"fake-etag"', "last_modified": "Wed, 21 Sep 2022 19:40:46 GMT",
                               "tool": {"name": "msdial_app.remote_archive", "version": "0.0.0-test"}}}
    return {"ok": False, "reason": "not_found", "message": "no such archive in the fixtures"}


class WrapperCase(unittest.TestCase):
    ACCESSIONS = ["ST002747"]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.campaign = self.tmp / "campaign"
        self.code_ref = records.Code.from_root(self.tmp / "code")
        self.code = rta.CodeSnapshot(self.code_ref.commit, self.code_ref.skill, self.code_ref.template, self.code_ref.root)
        self.catalog = FakeCatalog(self.tmp, self.ACCESSIONS + ["ST004606"])
        payloads: dict[str, Any] = {}
        for accession in self.ACCESSIONS + ["ST004606"]:
            payloads.update(te.to_v2(te.build_good(accession)))
        self.payloads = self.tmp / "payloads.json"
        self.payloads.write_text(json.dumps(payloads), encoding="utf-8")
        self.log = self.tmp / "claude-log.jsonl"
        self.fetched: list[str] = []
        self.ports = rta.Ports(
            catalog=self.catalog, lister=fake_lister, fetcher=self.fetch,
            servers={CATALOG: {"type": "stdio", "command": sys.executable, "args": ["-I", str(FAKE_MCP)]},
                     "msdial-interactive": {"type": "stdio", "command": "never-started"}},
            claude=rta.find_claude(str(FAKE_CLAUDE)), code=self.code)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def fetch(self, url: str) -> bytes:
        self.fetched.append(url)
        return f"#METABOLOMICS WORKBENCH synthetic mwTab for {url.split('/')[-3]}\n".encode("utf-8")

    def run_wrapper(self, scenario: str = "ok", *extra: str, accessions: list[str] | None = None) -> tuple[int, list[dict]]:
        argv = ["--campaign-dir", str(self.campaign), "--redact-term", "Quokka", "--timeout", "60"]
        for accession in accessions or self.ACCESSIONS:
            argv += ["--accession", accession]
        environment = {"FAKE_CLAUDE_SCENARIO": scenario, "FAKE_CLAUDE_PAYLOADS": str(self.payloads),
                       "FAKE_CLAUDE_LOG": str(self.log), "CLAUDECODE": "1"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(out):
            code = rta.main([*argv, *extra], ports=self.ports)
        return code, json.loads(out.getvalue())

    def triage_dir(self, accession: str = "ST002747") -> Path:
        return self.campaign / "triage" / accession

    def payload_files(self, accession: str = "ST002747") -> list[str]:
        return rta.existing_payloads(self.triage_dir(accession)) if self.triage_dir(accession).is_dir() else []

    def launches(self) -> list[dict[str, Any]]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def validate(self, accession: str = "ST002747") -> "vt.Report":
        inputs = self.triage_dir(accession) / "inputs"
        return vt.validate(self.triage_dir(accession), vt.read_catalog(inputs),
                           [m for tsv in sorted(inputs.glob("*.tsv")) for m in vt.read_listing(tsv)],
                           code_root=self.code.root)


class DryRunTests(WrapperCase):
    def test_dry_run_prepares_inputs_prompt_and_session_without_launching(self) -> None:
        code, [summary] = self.run_wrapper("ok", "--dry-run", "--analysis-purpose", "Compare infected and mock cells.")
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "prepared")
        self.assertEqual(self.launches(), [], "a dry run never launches the model")
        self.assertEqual(self.payload_files(), [])
        inputs = self.triage_dir() / "inputs"
        units = [u["unit_id"] for u in te.load("ST002747")[0]["units"]]
        self.assertEqual(summary["units"], units)
        for unit in units:
            handoff = json.loads((inputs / unit / "handoff.json").read_text(encoding="utf-8"))
            for key, name in (("sample_table_path", "samples.json"), ("file_manifest_path", "files.json"),
                              ("analysis_input_manifest_path", "inputs.json")):
                self.assertEqual(Path(handoff[key]), inputs / unit / name, "the copy points at its frozen sidecars")
                self.assertTrue((inputs / unit / name).is_file())
            self.assertIsNone(handoff["class_proposal"], "the triage's handoff carries no Class")
            prompt = (inputs / unit / "prompt.md").read_text(encoding="utf-8")
            self.assertNotIn("{{", prompt)
            self.assertIn("`../inputs`", prompt)
            self.assertIn("../out/<unit_id>.json", prompt)
            self.assertIn(", ".join(units), prompt)
            self.assertIn("Compare infected and mock cells.", prompt)
            self.assertFalse(curated._holds_absolute_path(prompt.encode("utf-8")), "the rendered prompt is released")
        self.assertEqual(len(list(inputs.glob("*.members.tsv"))), 1)
        self.assertEqual(len(list(inputs.glob("AN*.mwtab.txt"))), len(units))
        self.assertTrue(all("/rest/study/analysis_id/AN" in url for url in self.fetched))
        session = Path(summary["session_dir"])
        self.assertEqual(sorted(p.relative_to(session / "work").as_posix() for p in (session / "work").rglob("*")
                                if p.is_file()), [rta.SKILL_PATH], "the working directory holds only the skill")
        self.assertEqual((session / "work" / rta.SKILL_PATH).read_bytes(), self.code.skill)
        self.assertEqual(rta.tree_digest(session / "inputs"),
                         {k: v for k, v in rta.tree_digest(inputs).items() if not k.endswith("prompt.md")})
        self.assertEqual(list((session / "out").iterdir()), [])
        mcp = json.loads((session / "harness" / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(list(mcp["mcpServers"]), [CATALOG], "only the server the skill allows")

    def test_the_flags_are_the_clean_configuration(self) -> None:
        code, [summary] = self.run_wrapper("ok", "--dry-run", "--model", "some-model")
        self.assertEqual(code, 0, summary)
        flags = summary["harness_flags"]
        denied = ",".join(f"mcp__{CATALOG}__{t}" for t in ("msdial_catalog_save_class_proposal",
                                                            "msdial_catalog_reanalysis_handoff"))
        self.assertEqual(flags, [
            "-p", "--output-format", "stream-json", "--verbose", "--restricted",
            "--settings", "../harness/settings.json", "--strict-mcp-config", "--mcp-config", "../harness/mcp.json",
            "--tools", "Read,Glob,Grep,Write", "--allowedTools", ",".join(ALLOWED), "--disallowedTools", denied,
            "--permission-mode", "dontAsk", "--permission-prompts", "none", "--disable-slash-commands",
            "--no-session-persistence", "--session-id", "<session_id>", "--debug-file", "../harness/debug.log",
            "--add-dir", "../inputs", "../out", "--model", "some-model"])
        session = Path(summary["session_dir"])
        self.assertEqual(json.loads((session / "harness" / "settings.json").read_text(encoding="utf-8")),
                         {"disableAllHooks": True})
        command = summary["command"]
        self.assertEqual(command[command.index("--add-dir") + 1:command.index("--add-dir") + 3],
                         [str(session / "inputs"), str(session / "out")])


class SessionTests(WrapperCase):
    def test_clean_session_writes_payloads_and_records_and_the_validator_passes(self) -> None:
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "triaged")
        self.assertEqual(summary["verdict"], "pass")
        self.assertEqual(summary["configuration"], "clean")
        units = [u["unit_id"] for u in te.load("ST002747")[0]["units"]]
        self.assertEqual(summary["accepted"], units)
        [launch] = self.launches()
        self.assertEqual(launch["env"], {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                                         "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDECODE": None})
        self.assertEqual(Path(launch["cwd"]), Path(summary["session_dir"]) / "work")
        session_id = launch["argv"][launch["argv"].index("--session-id") + 1]
        session = Path(summary["session_dir"])
        record_schema = vt.load_schema(vt.RECORD_SCHEMA_ID)
        for unit in units:
            payload = self.triage_dir() / f"{unit}.json"
            self.assertEqual(payload.read_bytes(), (session / "out" / f"{unit}.json").read_bytes(), "byte for byte")
            raw = (self.triage_dir() / f"{unit}.record.json").read_bytes()
            record = json.loads(raw)
            self.assertEqual(vt.schema_errors(record, record_schema, record_schema), [])
            self.assertFalse(curated._holds_absolute_path(raw), "a record holds no absolute path")
            self.assertNotIn(b"quokka", raw.lower())
            self.assertEqual(record["decision_id"], sha(payload.read_bytes()))
            self.assertEqual(record["agent"]["session_id"], session_id)
            self.assertEqual(record["agent"]["harness"], "claude-code")
            self.assertEqual(record["agent"]["harness_version"], "0.0.0-fake")
            self.assertEqual(record["agent"]["model_id"], "fake-model-1", "the CLI default, as the transcript names it")
            context = record["agent"]["context"]
            self.assertEqual((context["configuration"], context["context_files"], context["memory"], context["hooks"]),
                             ("clean", [], "disabled", []))
            self.assertEqual(context["mcp_servers"], [{"name": CATALOG, "version": "0.6.1-test"}])
            self.assertIn("env CLAUDE_CODE_DISABLE_CLAUDE_MDS=1", context["harness_flags"])
            self.assertIn("--strict-mcp-config", context["harness_flags"])
            self.assertEqual(record["skill"]["repo_commit"], self.code.commit)
            self.assertEqual(record["skill"]["sha256"], sha(self.code.skill))
            self.assertEqual(record["prompt"]["template_sha256"], sha(self.code.template))
            self.assertEqual(record["transcript"]["unredacted_sha256"],
                             sha((session / "harness" / "transcript.jsonl").read_bytes()))
            self.assertIsNone(record["transcript"]["released_path"])
            first = record["inputs"][0]
            self.assertEqual((first["role"], first["path"], first["source"]),
                             ("catalog_handoff", f"inputs/{unit}/handoff.json", "msdial_catalog_reanalysis_handoff"))
            roles = [item["role"] for item in record["inputs"]]
            self.assertEqual(roles.count("catalog_handoff"), 1, "one handoff is the unit's own")
            listing = next(item for item in record["inputs"] if item["role"] == "archive_listing")
            self.assertEqual(listing["retrieval"]["method"], "remote_central_directory")
            self.assertEqual(listing["retrieval"]["range_bytes_read"], 17340)
            for item in record["inputs"]:
                data = (self.triage_dir() / item["path"]).read_bytes()
                self.assertEqual((item["sha256"], item["bytes"]), (sha(data), len(data)), item["path"])
            calls = record["tool_calls"]
            self.assertEqual([c["seq"] for c in calls], list(range(1, len(calls) + 1)))
            self.assertEqual([c["tool"] for c in calls],
                             ["Read", f"mcp__{CATALOG}__msdial_catalog_get_analysis_unit", *["Read"] * len(units),
                              *["Write"] * len(units)])
            mcp_call = calls[1]
            self.assertEqual(mcp_call["arguments"]["api_key"], "<redacted>")
            self.assertEqual(mcp_call["arguments"]["database"], "<external>/native.sqlite")
            self.assertEqual(calls[0]["arguments"]["file_path"].replace("\\", "/"), f"./{rta.SKILL_PATH}")
            self.assertEqual(calls[2]["arguments"]["file_path"].replace("\\", "/"), f"../inputs/{units[0]}/handoff.json")
            for call in calls:
                stored = (self.triage_dir() / call["result_path"]).read_bytes()
                self.assertEqual(call["result_sha256"], sha(stored))
                self.assertEqual(call["result_path"], f"inputs/{unit}/calls/{call['seq']}.json")
                self.assertFalse(curated._holds_absolute_path(stored), call["result_path"])
                self.assertNotIn(b"example.org", stored)
                self.assertNotIn(b"quokka", stored.lower())
            self.assertIn("<redacted>", (self.triage_dir() / calls[1]["result_path"]).read_text(encoding="utf-8"))
        report = self.validate()
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings if f.level != "note"])
        self.assertTrue((session / "harness" / "validator.json").is_file())

    def test_parallel_accessions_each_get_their_own_session(self) -> None:
        code, summaries = self.run_wrapper("ok", "--max-parallel", "2", accessions=["ST002747", "ST004606"])
        self.assertEqual(code, 0, summaries)
        self.assertEqual([s["verdict"] for s in summaries], ["pass", "pass"])
        self.assertEqual(len({s["session_dir"] for s in summaries}), 2)
        self.assertEqual(len(self.launches()), 2)

    def test_a_record_the_model_writes_refuses_the_whole_session(self) -> None:
        code, [summary] = self.run_wrapper("record")
        self.assertEqual(code, 3)
        self.assertEqual(summary["status"], "refused")
        self.assertTrue(any(".record.json" in p and "only the wrapper" in p for p in summary["problems"]), summary)
        self.assertEqual(self.payload_files(), [], "no payload is accepted")
        self.assertEqual(list(self.triage_dir().glob("*.record.json")), [])
        self.assertTrue((Path(summary["session_dir"]) / "harness" / "failure.json").is_file())

    def test_a_failed_session_leaves_no_payload_accepted(self) -> None:
        code, [summary] = self.run_wrapper("fail")
        self.assertEqual(code, 3)
        self.assertEqual(summary["status"], "refused")
        self.assertTrue(any("exited with status 1" in p for p in summary["problems"]), summary)
        self.assertTrue(any("error_during_execution" in p for p in summary["problems"]), summary)
        self.assertEqual(self.payload_files(), [])
        self.assertTrue(list((Path(summary["session_dir"]) / "out").glob("*.json")), "the model did write them")

    def test_a_timed_out_session_is_stopped_and_nothing_accepted(self) -> None:
        argv_timeout = ["--timeout", "3"]
        code, [summary] = self.run_wrapper("hang", *argv_timeout)
        self.assertEqual(code, 3)
        self.assertTrue(any("timed out" in p for p in summary["problems"]), summary)
        self.assertEqual(self.payload_files(), [])

    def test_a_write_outside_the_output_folder_refuses_the_session(self) -> None:
        code, [summary] = self.run_wrapper("outside")
        self.assertEqual(code, 3)
        self.assertTrue(any("wrote outside the output folder" in p for p in summary["problems"]), summary)
        self.assertTrue(any("working directory changed" in p for p in summary["problems"]), summary)
        self.assertEqual(self.payload_files(), [])

    def test_an_existing_payload_is_never_overwritten_and_nothing_launches(self) -> None:
        self.triage_dir().mkdir(parents=True)
        existing = self.triage_dir() / "AN004454.json"
        existing.write_bytes(b"{}")
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual(code, 3)
        self.assertIn("already triaged", summary["problems"][0])
        self.assertEqual(existing.read_bytes(), b"{}")
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.catalog.calls, [], "nothing is prepared for a triaged accession")

    def test_an_accession_the_catalog_lacks_fails_alone_and_leaves_no_staging(self) -> None:
        code, summaries = self.run_wrapper("ok", accessions=["ST999999", "ST002747"])
        self.assertEqual(code, 3)
        self.assertEqual([s["status"] for s in summaries], ["failed", "triaged"])
        self.assertIn("holds no analysis unit of ST999999", summaries[0]["problems"][0])
        self.assertEqual(list((self.campaign / "triage" / "ST999999").glob(".inputs-*")), [])
        self.assertEqual(summaries[1]["verdict"], "pass")

    def test_a_configuration_that_is_not_clean_is_recorded_as_such_and_held(self) -> None:
        code, [summary] = self.run_wrapper("unclean")
        self.assertEqual(code, 1, summary)
        self.assertEqual(summary["verdict"], "hold")
        self.assertTrue(summary["configuration"].startswith("not clean: plugins outside the harness"), summary)
        report = self.validate()
        self.assertTrue([f for f in report.findings if f.check == "T-CONTEXT" and f.level == "hold"])

    def test_tools_beyond_the_skill_left_in_the_session_are_not_clean(self) -> None:
        code, [summary] = self.run_wrapper("nodeny")
        self.assertEqual(code, 1, summary)
        self.assertIn("the harness offered tools the skill does not list", summary["configuration"])
        self.assertIn("msdial_catalog_save_class_proposal", summary["configuration"])

    def test_a_tool_the_skill_does_not_allow_is_a_validator_error(self) -> None:
        code, [summary] = self.run_wrapper("bash")
        self.assertEqual(code, 2, summary)
        self.assertEqual(summary["verdict"], "error")
        record = json.loads((self.triage_dir() / "AN004454.record.json").read_text(encoding="utf-8"))
        bash = [c for c in record["tool_calls"] if c["tool"] == "Bash"]
        self.assertEqual(bash[0]["status"], "error")
        report = self.validate()
        self.assertTrue([f for f in report.findings if f.check == "T-TOOLS" and f.level == "error" and "Bash" in f.message])


class PartTests(unittest.TestCase):
    def test_scrubber(self) -> None:
        session = Path("C:/campaign/triage-sessions/ST1/s1") if os.name == "nt" else Path("/campaign/triage-sessions/ST1/s1")
        scrub = rta.Scrubber.for_session(session, ["Jane Roe", "Roe"])
        base = str(session)
        self.assertEqual(scrub.text(base + os.sep + "work" + os.sep + "a.txt").replace("\\", "/"), "./a.txt")
        self.assertEqual(scrub.text(base + os.sep + "inputs" + os.sep + "AN1" + os.sep + "handoff.json").replace("\\", "/"),
                         "../inputs/AN1/handoff.json")
        self.assertEqual(scrub.text("open C:\\Users\\Jane Roe\\data\\x.sqlite now"), "open <external>/x.sqlite now")
        self.assertEqual(scrub.text('{"p": "D:\\\\a b\\\\c\\\\d.json"}'), '{"p": "<external>/d.json"}')
        self.assertEqual(scrub.text("\\\\server\\share\\dir\\f.raw"), "<external>/f.raw")
        self.assertEqual(scrub.text("https://example.org/a/b.zip"), "https://example.org/a/b.zip")
        self.assertEqual(scrub.text("key sk-ant-api03-ABCDEFGHIJKLMN and Bearer abcdefghijkl"), "key <redacted> and <redacted>")
        self.assertEqual(scrub.text("mail jane.roe@example.org"), "mail <redacted-email>")
        self.assertEqual(scrub.text("approved by roe on 11-06; Roebuck stays"), "approved by <redacted> on 11-06; Roebuck stays")
        self.assertEqual(scrub.value({"token": "abc", "nested": {"Password": "x", "n": 1}, "list": ["ok"]}),
                         {"token": "<redacted>", "nested": {"Password": "<redacted>", "n": 1}, "list": ["ok"]})

    def test_parse_transcript_pairs_results_and_counts_each_call_once(self) -> None:
        lines = [
            {"type": "system", "subtype": "init", "model": "m", "session_id": "s", "tools": ["Read"]},
            {"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "x"}}]}},
            {"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "x"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}},
            {"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "id": "b", "name": "Write", "input": {}}]}},
            {"type": "result", "subtype": "success", "is_error": False},
        ]
        data = ("\ufeff" + "\n".join(json.dumps(line) for line in lines) + "\nnot json\n").encode("utf-8")
        parsed = rta.parse_transcript(data)
        self.assertEqual([(c.ident, c.name) for c in parsed.calls], [("a", "Read"), ("b", "Write")])
        self.assertEqual(parsed.calls[0].result["content"], "ok")
        self.assertIsNone(parsed.calls[1].result)
        self.assertEqual(parsed.result["subtype"], "success")
        self.assertEqual(parsed.models, ["m"])
        self.assertEqual(len(parsed.problems), 1)

    def test_probe_mcp_reads_the_server_version_and_every_page_of_tools(self) -> None:
        answer = rta.probe_mcp({"type": "stdio", "command": sys.executable, "args": ["-I", str(FAKE_MCP)]}, timeout=60)
        self.assertEqual(answer, {"version": "0.6.1-test", "tools": SERVER_TOOLS, "problem": ""})
        self.assertIn("not a stdio server", rta.probe_mcp({"type": "http", "url": "https://x"})["problem"])

    def test_find_claude_runs_a_python_fake_with_this_interpreter(self) -> None:
        self.assertEqual(rta.find_claude(str(FAKE_CLAUDE)), [sys.executable, "-I", str(FAKE_CLAUDE)])
        self.assertEqual(rta.find_claude("C:/x/claude.exe"), [str(Path("C:/x/claude.exe"))])

    def test_accessions_from_options_and_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            listed = Path(folder) / "acc.txt"
            listed.write_text("# pool\nST002747\nST004606  first pool\n\nST002747\n", encoding="utf-8")
            self.assertEqual(rta.read_accessions(["ST003128, ST002283"], listed),
                             ["ST003128", "ST002283", "ST002747", "ST004606"])
            with self.assertRaises(SystemExit):
                rta.read_accessions(["../x"], None)
            with self.assertRaises(SystemExit):
                rta.read_accessions([], None)

    def test_the_prompt_template_is_filled_whole(self) -> None:
        template = (ROOT / rta.TEMPLATE_PATH).read_bytes()
        rendered = rta.render_prompt(template, accession="ST1", unit_ids="AN1", input_dir="../inputs",
                                     output_dir="../out", analysis_purpose="null")
        self.assertNotIn(b"{{", rendered)
        with self.assertRaises(rta.WrapperError):
            rta.render_prompt(template, accession="ST1")

    def test_the_wrapper_reads_the_skills_allowed_tools(self) -> None:
        code = rta.CodeSnapshot("c" * 40, (ROOT / rta.SKILL_PATH).read_bytes(), b"")
        self.assertEqual(code.allowed_tools, ALLOWED)
        self.assertEqual(code.skill_version, "1.1.0")


if __name__ == "__main__":
    unittest.main()
