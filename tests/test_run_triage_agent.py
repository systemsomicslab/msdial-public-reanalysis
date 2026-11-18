"""scripts/run-triage-agent.py: the triage wrapper of docs/llm-assisted-curation.md section 2.3 ("Context" and the
envelope).

The harness is tests/fake_claude.py, which takes the wrapper's real flags, emits a canned stream-JSON transcript and
writes the spec it is given (skill 1.2.0: the agent writes one spec, code expands it into the payloads); the MCP server
is the real spec check server (scripts/triage-spec-server.py, written into the session's code tree); the Catalog, the
remote archive listing and the mwTab fetch are in-process fakes over the eval fixtures. Checked: the inputs, the digest
and the session prepared (dry run included), the clean-configuration flags, the spec expanded and the envelope written
from the transcript with its derivation (hashes the validator recomputes, tool calls in order with secrets, e-mail
addresses, the operator's names and absolute paths removed, the results stored), a configuration that is not clean
recorded as such and held, and every refusal: a payload, a decision record or any other file the model writes, a spec
that does not expand, a write outside the output folder, a failed or timed-out session, an existing payload.
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
SPEC_SERVER = "msdial-triage-spec"
SERVER_TOOLS = ["msdial_catalog_status", "msdial_catalog_search", "msdial_catalog_get_analysis_unit",
                "msdial_catalog_save_class_proposal", "msdial_catalog_reanalysis_handoff"]
ALLOWED = ["Read", "Glob", "Grep", "Write", f"mcp__{SPEC_SERVER}__triage_spec_check"]


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
        self.code = self.code_ref.snapshot(rta)
        self.catalog = FakeCatalog(self.tmp, self.ACCESSIONS + ["ST004606"])
        payloads: dict[str, Any] = {}
        for accession in self.ACCESSIONS + ["ST004606"]:
            payloads.update(te.to_v2(te.build_good(accession)))
        self.payloads = self.tmp / "payloads.json"
        self.payloads.write_text(json.dumps(payloads), encoding="utf-8")
        self.specs = self.tmp / "specs.json"
        self.specs.write_text(json.dumps({a: json.loads(te.spec_path(a).read_text(encoding="utf-8"))
                                          for a in self.ACCESSIONS + ["ST004606"]}), encoding="utf-8")
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
                       "FAKE_CLAUDE_SPECS": str(self.specs), "FAKE_CLAUDE_LOG": str(self.log), "CLAUDECODE": "1"}
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
            self.assertIn("`../out/ST002747.spec.json`", prompt)
            self.assertIn("`../inputs/digest.md`", prompt)
            self.assertIn(", ".join(units), prompt)
            self.assertIn("Compare infected and mock cells.", prompt)
            self.assertFalse(curated._holds_absolute_path(prompt.encode("utf-8")), "the rendered prompt is released")
        self.assertEqual(len(list(inputs.glob("*.members.tsv"))), 1)
        digest = (inputs / "digest.md").read_bytes()
        self.assertIn(b"# Triage digest: ST002747", digest)
        self.assertFalse(curated._holds_absolute_path(digest), "the digest is released")
        self.assertEqual(len(list(inputs.glob("AN*.mwtab.txt"))), len(units))
        self.assertTrue(all("/rest/study/analysis_id/AN" in url for url in self.fetched))
        session = Path(summary["session_dir"])
        self.assertEqual(sorted(p.relative_to(session / "work").as_posix() for p in (session / "work").rglob("*")
                                if p.is_file()), sorted([rta.SKILL_PATH, rta.SCHEMA_PATH]),
                         "the working directory holds only the skill and the spec schema it names")
        self.assertEqual(rta.SCHEMA_PATH, "schemas/msdial-unit-triage-spec.v1.json")
        self.assertEqual((session / "work" / rta.SKILL_PATH).read_bytes(), self.code.skill)
        self.assertEqual((session / "work" / rta.SCHEMA_PATH).read_bytes(), self.code.schema)
        self.assertEqual(rta.tree_digest(session / "inputs"),
                         {k: v for k, v in rta.tree_digest(inputs).items() if not k.endswith("prompt.md")})
        self.assertEqual(list((session / "out").iterdir()), [])
        mcp = json.loads((session / "harness" / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(list(mcp["mcpServers"]), [SPEC_SERVER], "only the spec check server the skill allows")
        server = mcp["mcpServers"][SPEC_SERVER]
        self.assertEqual(Path(server["args"][1]), session / "code" / rta.SPEC_SERVER_PATH)
        self.assertEqual((session / "code" / rta.EXPANDER_PATH).read_bytes(), self.code.files[rta.EXPANDER_PATH],
                         "the session's code is the commit's")

    def test_the_flags_are_the_clean_configuration(self) -> None:
        code, [summary] = self.run_wrapper("ok", "--dry-run", "--model", "some-model", "--effort", "medium")
        self.assertEqual(code, 0, summary)
        flags = summary["harness_flags"]
        self.assertEqual(flags, [
            "-p", "--output-format", "stream-json", "--verbose", "--restricted",
            "--settings", "../harness/settings.json", "--strict-mcp-config", "--mcp-config", "../harness/mcp.json",
            "--tools", "Read,Glob,Grep,Write", "--allowedTools", ",".join(ALLOWED),
            "--permission-mode", "dontAsk", "--permission-prompts", "none", "--disable-slash-commands",
            "--no-session-persistence", "--session-id", "<session_id>", "--debug-file", "../harness/debug.log",
            "--add-dir", "../inputs", "../out", "--model", "some-model", "--effort", "medium"])
        session = Path(summary["session_dir"])
        self.assertEqual(json.loads((session / "harness" / "settings.json").read_text(encoding="utf-8")),
                         {"disableAllHooks": True})
        command = summary["command"]
        self.assertEqual(command[command.index("--add-dir") + 1:command.index("--add-dir") + 3],
                         [str(session / "inputs"), str(session / "out")])


class SessionTests(WrapperCase):
    def test_clean_session_writes_payloads_and_records_and_the_validator_passes(self) -> None:
        code, [summary] = self.run_wrapper("ok")
        session = Path(summary["session_dir"])
        spec = self.triage_dir() / "ST002747.spec.json"
        self.assertEqual(spec.read_bytes(), (session / "out" / "ST002747.spec.json").read_bytes(), "the spec, byte for byte")
        self.assertEqual(json.loads((session / "harness" / "expansion.json").read_text(encoding="utf-8"))["findings"], [])
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
            self.assertFalse((session / "out" / f"{unit}.json").exists(), "code writes the payloads, the model the spec")
            raw = (self.triage_dir() / f"{unit}.record.json").read_bytes()
            self.assertLess(len(raw), 100_000, "the only Write argument is the spec")
            record = json.loads(raw)
            derivation = record["derivation"]
            self.assertEqual(derivation["spec"], {"path": "ST002747.spec.json", "schema": "msdial-unit-triage-spec.v1",
                                                  "sha256": sha(spec.read_bytes()), "bytes": len(spec.read_bytes())})
            self.assertEqual(derivation["expander"]["sha256"], sha(self.code.files[rta.EXPANDER_PATH]))
            self.assertEqual(derivation["digest"]["path"], "inputs/digest.md")
            self.assertEqual(derivation["context"], {"decided_at": record["finished_at"], "model": "fake-model-1",
                                                     "agent": "claude-code", "skill_version": "1.2.0", "purpose": None})
            document = json.loads(payload.read_bytes())
            self.assertEqual((document["decided_at"], document["derivation"]["spec_sha256"]),
                             (record["finished_at"], sha(spec.read_bytes())))
            self.assertIn({"role": "triage_digest", "path": "inputs/digest.md"},
                          [{k: i[k] for k in ("role", "path")} for i in record["inputs"]])
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
            self.assertEqual(context["mcp_servers"], [{"name": SPEC_SERVER, "version": "1.0.0"}])
            self.assertIn("env CLAUDE_CODE_DISABLE_CLAUDE_MDS=1", context["harness_flags"])
            self.assertIn("--strict-mcp-config", context["harness_flags"])
            self.assertEqual(record["skill"]["repo_commit"], self.code.commit)
            self.assertEqual(record["skill"]["sha256"], sha(self.code.skill))
            self.assertEqual(record["prompt"]["template_sha256"], sha(self.code.template))
            self.assertEqual(record["transcript"]["unredacted_sha256"],
                             sha((session / "harness" / "transcript.jsonl").read_bytes()))
            released = self.triage_dir() / record["transcript"]["released_path"]
            self.assertTrue(record["transcript"]["released_path"].startswith("transcripts/ST002747-"))
            self.assertEqual(record["transcript"]["sha256"], sha(released.read_bytes()))
            self.assertFalse(curated._holds_absolute_path(released.read_bytes()), "the released transcript")
            self.assertNotIn(b"quokka", released.read_bytes().lower())
            self.assertNotIn(b"example.org", released.read_bytes())
            self.assertEqual(record["wrapper"], {"path": rta.WRAPPER_PATH, "sha256": sha(self.code.wrapper),
                                                 "repo_commit": self.code.commit})
            handoff = self.triage_dir() / "inputs" / unit / "handoff.json"
            self.assertEqual(json.loads(payload.read_bytes())["catalog_handoff_sha256"], sha(handoff.read_bytes()),
                             "code writes the hash of the handoff copy")
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
            self.assertEqual([c["tool"] for c in calls], ["Read", "Grep", "Read", *["Read"] * len(units), "Write"])
            secret_call = calls[1]
            self.assertEqual(secret_call["arguments"]["api_key"], "<redacted>")
            self.assertEqual(secret_call["arguments"]["database"], "<external>/native.sqlite")
            self.assertEqual(calls[0]["arguments"]["file_path"].replace("\\", "/"), f"./{rta.SKILL_PATH}")
            self.assertEqual(calls[2]["arguments"]["file_path"].replace("\\", "/"), "../inputs/digest.md")
            self.assertEqual(calls[3]["arguments"]["file_path"].replace("\\", "/"), f"../inputs/{units[0]}/handoff.json")
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
        # The wrapper's validation ran with --emit-selection: one validator_verdict per decision in the review log, so
        # the plan's review_log_head_sha256 is set.
        log, problems = vt.read_review_log(self.triage_dir())
        self.assertEqual(problems, [])
        self.assertEqual(sorted(e["decision_id"] for e in log if e["kind"] == "validator_verdict"),
                         sorted(sha((self.triage_dir() / f"{u}.json").read_bytes()) for u in units))
        self.assertIsNotNone(curated.review_log_head(self.triage_dir()))

    def test_a_crlf_checkout_renders_a_prompt_the_session_reads(self) -> None:
        # core.autocrlf=true gives the working tree's template CRLF; the unit list and hashes still parse.
        template = self.code.template.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.code = rta.CodeSnapshot(self.code.commit, self.code.skill, template, self.code.root,
                                     wrapper=self.code.wrapper, wrapper_commit=self.code.commit,
                                     revision_template=self.code.revision_template, schema=self.code.schema,
                                     files=self.code.files)
        (self.code.root / rta.TEMPLATE_PATH).write_bytes(template)
        self.ports.code = self.code
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual((code, summary["verdict"]), (0, "pass"), summary)

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
        self.assertIn("write_anything", summary["configuration"])

    def test_the_catalog_server_is_not_in_the_session(self) -> None:
        code, [summary] = self.run_wrapper("ok", "--dry-run")
        self.assertEqual(code, 0, summary)
        self.assertNotIn(CATALOG, " ".join(summary["command"]), "every Catalog fact arrives as a hashed input")

    def test_a_payload_the_model_writes_refuses_the_session(self) -> None:
        code, [summary] = self.run_wrapper("payload")
        self.assertEqual(code, 3)
        self.assertEqual(summary["status"], "refused")
        self.assertTrue(any("AN004454.json, which is not a payload this session was asked for" in p
                            for p in summary["problems"]), summary)
        self.assertEqual((self.payload_files(), list(self.triage_dir().glob("*.spec.json"))), ([], []))

    def test_a_spec_that_does_not_expand_refuses_the_session(self) -> None:
        code, [summary] = self.run_wrapper("badspec")
        self.assertEqual(code, 3)
        self.assertEqual(summary["status"], "refused")
        self.assertTrue(any("does not expand" in p for p in summary["problems"]), summary)
        expansion = json.loads((Path(summary["session_dir"]) / "harness" / "expansion.json").read_text(encoding="utf-8"))
        self.assertFalse(expansion["ok"])
        self.assertTrue(any(f["code"].startswith("X-UNCOVERED") for f in expansion["findings"]), expansion)
        self.assertEqual((self.payload_files(), list(self.triage_dir().glob("*.json"))), ([], []), "nothing is committed")
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual((code, summary["verdict"]), (0, "pass"), "the accession is simply run again")

    def test_the_spec_check_is_called_and_its_result_stored(self) -> None:
        code, [summary] = self.run_wrapper("check")
        self.assertEqual((code, summary["verdict"]), (0, "pass"), summary)
        record = json.loads((self.triage_dir() / "AN004454.record.json").read_text(encoding="utf-8"))
        [check] = [c for c in record["tool_calls"] if c["tool"] == f"mcp__{SPEC_SERVER}__triage_spec_check"]
        self.assertEqual((check["status"], check["arguments"]), ("ok", {"spec_file": "../out/ST002747.spec.json"}))
        stored = json.loads((self.triage_dir() / check["result_path"]).read_text(encoding="utf-8"))
        answer = json.loads(stored["content"][0]["text"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["counts"]["AN004454"]["rows_kept"], 13)
        self.assertFalse(curated._holds_absolute_path(json.dumps(stored).encode("utf-8")))
        report = self.validate()
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings if f.level != "note"])

    def test_an_mwtab_text_quoting_a_path_is_offered_portable_and_releasable(self) -> None:
        original = b"#METABOLOMICS WORKBENCH\nMS_COMMENTS\tmethod C:\\Xcalibur\\methods\\x.meth\n"
        self.fetch = lambda url: original  # type: ignore[method-assign]
        self.ports.fetcher = self.fetch
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual((code, summary["verdict"]), (0, "pass"), summary)
        text = next((self.triage_dir() / "inputs").glob("AN*.mwtab.txt"))
        self.assertFalse(curated._holds_absolute_path(text.read_bytes()))
        record = json.loads((self.triage_dir() / "AN004454.record.json").read_text(encoding="utf-8"))
        [entry] = [i for i in record["inputs"] if i["path"] == f"inputs/{text.name}"]
        self.assertEqual(entry["original_sha256"], sha(original))
        self.assertIn("<external>/<file name>", entry["redaction"])

    def test_a_read_outside_the_session_refuses_it(self) -> None:
        code, [summary] = self.run_wrapper("readout")
        self.assertEqual(code, 3)
        self.assertTrue(any("read outside the session's folders (MEMORY.md)" in p for p in summary["problems"]), summary)
        self.assertEqual(self.payload_files(), [])

    def test_a_spec_holding_an_absolute_path_refuses_the_session(self) -> None:
        code, [summary] = self.run_wrapper("pathpay")
        self.assertEqual(code, 3)
        self.assertTrue(any("holds an absolute path" in p for p in summary["problems"]), summary)
        self.assertEqual(self.payload_files(), [], "nothing immutable is written, so the accession can be triaged again")
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual((code, summary["verdict"]), (0, "pass"), summary)

    def test_without_the_debug_log_hooks_and_memory_are_not_clean(self) -> None:
        code, [summary] = self.run_wrapper("nodebug")
        self.assertEqual(code, 1, summary)
        self.assertIn("the harness wrote no debug log", summary["configuration"])
        record = json.loads((self.triage_dir() / "AN004454.record.json").read_text(encoding="utf-8"))
        self.assertNotEqual(record["agent"]["context"]["memory"], "disabled")
        self.assertIsNone(record["transcript"]["released_path"], "a run that is not clean is not released")

    def test_a_tool_the_skill_does_not_allow_is_a_validator_error(self) -> None:
        code, [summary] = self.run_wrapper("bashran")
        self.assertEqual(code, 2, summary)
        self.assertEqual(summary["verdict"], "error")
        record = json.loads((self.triage_dir() / "AN004454.record.json").read_text(encoding="utf-8"))
        bash = [c for c in record["tool_calls"] if c["tool"] == "Bash"]
        self.assertEqual(bash[0]["status"], "ok")
        report = self.validate()
        self.assertTrue([f for f in report.findings if f.check == "T-TOOLS" and f.level == "error" and "Bash" in f.message])

    def test_a_tool_the_harness_refused_is_a_note(self) -> None:
        # The 2026-11-18 re-pilot asked for Edit, which the harness does not offer; the refusal changed nothing.
        code, [summary] = self.run_wrapper("bash")
        self.assertEqual((code, summary["verdict"]), (0, "pass"), summary)
        record = json.loads((self.triage_dir() / "AN004454.record.json").read_text(encoding="utf-8"))
        bash = [c for c in record["tool_calls"] if c["tool"] == "Bash"]
        self.assertEqual(bash[0]["status"], "error")
        report = self.validate()
        self.assertFalse([f for f in report.findings if f.check == "T-TOOLS" and f.level == "error"])
        self.assertTrue([f for f in report.findings if f.check == "T-TOOLS" and f.level == "note" and "Bash" in f.message])


class RobustnessTests(WrapperCase):
    def test_two_runs_of_one_accession_never_share_its_directory(self) -> None:
        # The same accession twice in one batch, in another case: NTFS folds case, so both are one directory. One run
        # triages it; the other is never started (one accession once, whatever its case) or refused by the lock.
        code, summaries = self.run_wrapper("ok", "--max-parallel", "2", accessions=["ST002747", "st002747"])
        self.assertEqual(len(summaries), 1, "the batch names ST002747 once")
        self.assertEqual((code, summaries[0]["verdict"]), (0, "pass"), summaries)
        self.triage_dir().mkdir(parents=True, exist_ok=True)
        other = self.campaign / "triage" / "ST004606"
        other.mkdir(parents=True)
        (other / rta.LOCK_NAME).write_text("{}", encoding="utf-8")
        code, [summary] = self.run_wrapper("ok", accessions=["ST004606"])
        self.assertEqual(code, 3)
        self.assertIn("another run holds", summary["problems"][0])
        self.assertEqual(rta.existing_payloads(other), [])
        self.assertTrue((other / rta.LOCK_NAME).is_file(), "another run's lock is never removed")

    def test_a_leftover_record_stops_the_accession_before_anything_is_prepared(self) -> None:
        self.triage_dir().mkdir(parents=True)
        (self.triage_dir() / "AN004455.record.json").write_text("{}", encoding="utf-8")
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual(code, 3)
        self.assertIn("already triaged (AN004455.record.json)", summary["problems"][0])
        self.assertEqual((self.launches(), self.catalog.calls), ([], []))

    def test_a_commit_is_all_or_nothing(self) -> None:
        folder = self.tmp / "commit"
        folder.mkdir()
        (folder / "b.json").write_bytes(b"theirs")
        with self.assertRaises(rta.WrapperError) as raised:
            rta.commit_files(folder, [("inputs/x/calls/1.json", b"1"), ("a.json", b"a"), ("b.json", b"b"), ("c.json", b"c")])
        self.assertIn("nothing was kept", str(raised.exception))
        self.assertEqual(sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()), ["b.json"])
        self.assertEqual((folder / "b.json").read_bytes(), b"theirs")

    def test_a_validator_that_times_out_leaves_the_accepted_triage_as_it_is(self) -> None:
        real = rta.subprocess.run

        def run(command, *args, **kwargs):
            if str(rta.VALIDATOR_PATH) in [str(c) for c in command]:
                raise rta.subprocess.TimeoutExpired(command, 900)
            return real(command, *args, **kwargs)

        with mock.patch.object(rta.subprocess, "run", run):
            code, [summary] = self.run_wrapper("ok")
        self.assertEqual(code, 2, summary)
        self.assertEqual((summary["status"], summary["verdict"]), ("triaged", "error"))
        self.assertEqual(len(summary["accepted"]), 4)
        self.assertTrue((Path(summary["session_dir"]) / "harness" / "validator.json").is_file())

    def test_the_timeout_runs_while_the_prompt_is_written(self) -> None:
        # A prompt larger than a pipe holds, to a CLI that does not read it: the timeout still stops the session.
        started = rta.time.monotonic()
        code, [summary] = self.run_wrapper("slowstdin", "--timeout", "3", "--analysis-purpose", "x" * 60000)
        self.assertLess(rta.time.monotonic() - started, 20)
        self.assertEqual(code, 3)
        self.assertTrue(any("timed out" in p for p in summary["problems"]), summary)

    def test_a_missing_harness_fails_the_batch_before_anything_is_prepared(self) -> None:
        self.ports.claude = [str(self.tmp / "nowhere" / "claude.exe")]
        code, summaries = self.run_wrapper("ok", accessions=["ST002747", "ST004606"])
        self.assertEqual(code, 3)
        self.assertEqual([s["status"] for s in summaries], ["failed", "failed"])
        self.assertTrue(all("the harness could not be run" in s["problems"][0] for s in summaries), summaries)
        self.assertEqual((self.catalog.calls, self.fetched), ([], []))
        with self.assertRaises(rta.WrapperError):
            rta.find_claude(str(self.tmp / "nowhere" / "claude.exe"))


class RevisionTests(WrapperCase):
    UNIT = "AN004454"

    def set_spec(self, change) -> None:
        specs = json.loads(self.specs.read_text(encoding="utf-8"))
        change(specs["ST002747"]["units"][self.UNIT])
        self.specs.write_text(json.dumps(specs), encoding="utf-8")

    def answer(self, *extra: str) -> tuple[int, str]:
        script = importlib.util.spec_from_file_location("record_person_answer_under_test",
                                                        ROOT / "scripts" / "record-person-answer.py")
        module = importlib.util.module_from_spec(script)
        script.loader.exec_module(module)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main([str(self.triage_dir()), "--unit", self.UNIT, "--json", *extra])
        return code, out.getvalue() + err.getvalue()

    def test_a_question_answered_before_the_plan_is_settled_by_a_revision(self) -> None:
        def ask(unit):
            unit["status"] = "needs_person"
            unit["flags"] = [{"code": "mixed_acquisition", "certainty": "suspected",
                              "detail": "Samples and blanks may come from different batches."}]
            unit["needs_person"] = [{"code": "mixed_acquisition", "question": "Do the acquisitions form one run?"}]
        self.set_spec(ask)
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual((code, summary["verdict"]), (1, "hold"), summary)
        base = sha((self.triage_dir() / f"{self.UNIT}.json").read_bytes())
        others = {u: sha((self.triage_dir() / f"{u}.json").read_bytes()) for u in ("AN004455", "AN004456", "AN004457")}
        # The question is shown; an answer without a named person, or to a question not asked, is refused.
        code, shown = self.answer()
        self.assertEqual(code, 0)
        self.assertIn("mixed_acquisition", json.loads(shown)["questions"])
        self.assertEqual(self.answer("--question-code", "mixed_acquisition", "--answer", "One run.")[0], 2)
        self.assertEqual(self.answer("--question-code", "ms1_only", "--by", "Synthetic Reviewer", "--answer", "x")[0], 2)
        self.assertEqual(self.answer("--question-code", "mixed_acquisition", "--by", "Synthetic Reviewer", "--answer", "x",
                                     "--decision-id", "0" * 64)[0], 2)
        code, written = self.answer("--question-code", "mixed_acquisition", "--by", "Synthetic Reviewer",
                                    "--answer", "One run; the batches are comparable.", "--decision-id", base)
        self.assertEqual(code, 0, written)
        seq = json.loads(written)["seq"]

        # A revision that also changes a unit it does not name is refused (X-SCOPE), and nothing is committed.
        def settle(unit):
            unit["status"] = "ready"
            unit["needs_person"] = []
            unit["rationale"] = "The person's answer makes the acquisitions one run."
        self.set_spec(settle)
        specs = json.loads(self.specs.read_text(encoding="utf-8"))
        specs["ST002747"]["units"]["AN004455"]["rationale"] = "Changed without a cause."
        self.specs.write_text(json.dumps(specs), encoding="utf-8")
        code, [summary] = self.run_wrapper("ok", "--revise", f"{self.UNIT}={seq}")
        self.assertEqual(code, 3, summary)
        self.assertTrue(any("X-SCOPE AN004455" in p for p in summary["problems"]), summary)
        self.assertFalse((self.triage_dir() / "ST002747.spec.r1.json").exists())
        specs["ST002747"]["units"]["AN004455"]["rationale"] = json.loads(te.spec_path("ST002747").read_text(
            encoding="utf-8"))["units"]["AN004455"]["rationale"]
        self.specs.write_text(json.dumps(specs), encoding="utf-8")

        # The revision: ready, the flag kept, the question answered; only the named unit changes.
        code, [summary] = self.run_wrapper("ok", "--revise", f"{self.UNIT}={seq}")
        self.assertEqual((code, summary["status"], summary["verdict"]), (0, "triaged", "pass"), summary)
        self.assertEqual(summary["accepted"], [f"{self.UNIT}.r1"])
        self.assertEqual(summary["spec"]["path"], "ST002747.spec.r1.json")
        revision = self.triage_dir() / f"{self.UNIT}.r1.json"
        record = json.loads((self.triage_dir() / f"{self.UNIT}.r1.record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["supersedes"], base)
        self.assertEqual(record["payload"]["path"], revision.name)
        self.assertEqual(record["prompt"]["template_path"], rta.REVISION_TEMPLATE_PATH)
        self.assertEqual(record["derivation"]["spec"]["path"], "ST002747.spec.r1.json")
        paths = {item["path"] for item in record["inputs"] if item["role"] == "prior_decision"}
        self.assertEqual(paths, {f"inputs/{self.UNIT}/r1/previous.json", "inputs/spec.r1/previous.spec.json"})
        roles = {item["role"]: item["path"] for item in record["inputs"]}
        self.assertEqual(roles["review_entry"], f"inputs/{self.UNIT}/r1/cause.json")
        self.assertEqual((self.triage_dir() / f"inputs/{self.UNIT}/r1/previous.json").read_bytes(),
                         (self.triage_dir() / f"{self.UNIT}.json").read_bytes())
        self.assertEqual({u: sha((self.triage_dir() / f"{u}.json").read_bytes()) for u in others}, others)
        self.assertEqual([p.name for p in self.triage_dir().glob("AN004455*.json")], ["AN004455.json", "AN004455.record.json"])
        log, problems = vt.read_review_log(self.triage_dir())
        self.assertEqual(problems, [])
        [entry] = [e for e in log if e["kind"] == "revision"]
        self.assertEqual((entry["supersedes"], entry["new_decision_id"], entry["cause"]),
                         (base, sha(revision.read_bytes()), {"kind": "person_answer", "seq": seq}))
        report = self.validate()
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings if f.level != "note"])
        # The decision log walks: the revision's cause is the person's answer about the payload it revises.
        verify = importlib.util.spec_from_file_location("verify_decision_log_under_test",
                                                        ROOT / "scripts" / "verify-decision-log.py")
        module = importlib.util.module_from_spec(verify)
        verify.loader.exec_module(module)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main([str(self.campaign)]), 0)
        # One cause revises once.
        code, [summary] = self.run_wrapper("ok", "--revise", f"{self.UNIT}={seq}")
        self.assertEqual(code, 3)
        self.assertIn("already caused a revision", summary["problems"][0])
        self.assertFalse((self.triage_dir() / f"{self.UNIT}.r2.json").exists())

    def test_a_revision_needs_a_cause_about_the_payload(self) -> None:
        code, [summary] = self.run_wrapper("ok")
        self.assertEqual(code, 0, summary)
        code, [summary] = self.run_wrapper("ok", "--revise", f"{self.UNIT}=99")
        self.assertEqual(code, 3)
        self.assertIn("has no entry 99", summary["problems"][0])
        log, _ = vt.read_review_log(self.triage_dir())
        other = next(e for e in log if e["decision_id"] != sha((self.triage_dir() / f"{self.UNIT}.json").read_bytes()))
        code, [summary] = self.run_wrapper("ok", "--revise", f"{self.UNIT}={other['seq']}")
        self.assertIn("another decision", summary["problems"][0])
        with self.assertRaises(SystemExit):
            self.run_wrapper("ok", "--revise", f"{self.UNIT}=1", accessions=["ST002747", "ST004606"])

    def test_a_hand_written_payload_is_not_revised_by_a_spec(self) -> None:
        folder = self.triage_dir()
        te.write(te.to_v2(te.build_good("ST002747"), skill_version="1.1.0"), folder)
        code, [summary] = self.run_wrapper("ok", "--revise", f"{self.UNIT}=1")
        self.assertEqual(code, 3)
        self.assertIn("has no spec", summary["problems"][0])
        self.assertIn("new campaign directory", summary["problems"][0])


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
        self.assertEqual(scrub.text("seen in /home/jane/data/x.raw"), "seen in <external>/x.raw")

    def test_names_are_removed_in_the_forms_people_write_them(self) -> None:
        with mock.patch.dict(os.environ, {"USERNAME": "Jane Roe", "USER": "", "LOGNAME": ""}):
            terms = rta.operator_terms()
        for handle in ("jroe", "roej", "janeroe"):
            self.assertIn(handle, [t.casefold() for t in terms])
        scrub = rta.Scrubber.for_session(Path("C:/s") if os.name == "nt" else Path("/s"), terms)
        self.assertEqual(scrub.text("owner jroe; G:\\My Drive\\workshop_jroe\\a.txt; pool-roe2026; Roebuck"),
                         "owner <redacted>; <external>/a.txt; pool-<redacted>2026; Roebuck")
        self.assertIn("an operator's name", scrub.found("approved by roe"))
        self.assertEqual(scrub.found("a clean rationale"), [])
        # A name in a script without spaces between words is kept at any length.
        with mock.patch.dict(os.environ, {"USERNAME": "津川 博", "USER": "", "LOGNAME": ""}):
            terms = rta.operator_terms()
        self.assertTrue({"津川", "博"} <= set(terms), terms)
        scrub = rta.Scrubber.for_session(Path("C:/s") if os.name == "nt" else Path("/s"), terms)
        self.assertEqual(scrub.text("lock stage3b-poola-20261106-津川; also 津川"), "lock stage3b-poola-20261106-<redacted>; "
                         "also <redacted>")

    def test_a_repository_text_with_an_absolute_path_is_made_portable_before_the_session(self) -> None:
        text = b"#MS\nMS_COMMENTS\tmethod C:\\Xcalibur\\methods\\x.meth, data \\\\nas\\share\\run\\a.raw\n"
        released, extra = rta.release_safe_text(text)
        self.assertFalse(curated._holds_absolute_path(released))
        self.assertIn(b"<external>/x.meth", released)
        self.assertEqual(extra["original_sha256"], sha(text))
        self.assertEqual(rta.release_safe_text(b"no path here\n"), (b"no path here\n", {}))

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
        with self.assertRaises(rta.WrapperError):
            rta.find_claude("C:/nowhere/claude.exe")

    def test_accessions_from_options_and_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            listed = Path(folder) / "acc.txt"
            listed.write_text("# pool\nST002747\nST004606  first pool\n\nST002747\n", encoding="utf-8")
            self.assertEqual(rta.read_accessions(["ST003128, ST002283", "st003128"], listed),
                             ["ST003128", "ST002283", "ST002747", "ST004606"])
            self.assertEqual(rta.read_revisions(["AN1=3", "AN2=12"]), {"AN1": 3, "AN2": 12})
            for bad in (["AN1"], ["AN1=0"], ["AN1=2", "AN1=3"]):
                with self.assertRaises(SystemExit):
                    rta.read_revisions(bad)
            with self.assertRaises(SystemExit):
                rta.read_accessions(["../x"], None)
            with self.assertRaises(SystemExit):
                rta.read_accessions([], None)

    def test_the_prompt_template_is_filled_whole(self) -> None:
        template = (ROOT / rta.TEMPLATE_PATH).read_bytes()
        rendered = rta.render_prompt(template, accession="ST1", unit_ids="AN1", input_dir="../inputs",
                                     output_dir="../out", analysis_purpose="null", spec_name="ST1.spec.json")
        self.assertNotIn(b"{{", rendered)
        with self.assertRaises(rta.WrapperError):
            rta.render_prompt(template, accession="ST1")

    def test_the_wrapper_reads_the_skills_allowed_tools(self) -> None:
        code = rta.CodeSnapshot("c" * 40, (ROOT / rta.SKILL_PATH).read_bytes(), b"")
        self.assertEqual(code.allowed_tools, ALLOWED)
        self.assertEqual(code.skill_version, "1.2.0")


if __name__ == "__main__":
    unittest.main()
