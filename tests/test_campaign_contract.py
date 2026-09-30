"""The runner's contract with the real Interactive and Catalog code: names, parameters, result shapes.

The runner calls Interactive's MCP tool functions in-process and the Catalog's functions directly, in
two repositories merged independently of this one. Everything the fake ports in the other tests assume
about them is checked here against the real modules: that MCPServer().tool() hands the function back
unchanged, that every parameter the ports pass is one the function takes, that a refusal comes back as
the ok:false shape policy.classify_result reads, and that the records the runner writes (the campaign
authorization, the Class ratification, the run record) are the ones the other side accepts.

No backend is started and nothing leaves the process: the one HTTP helper the tools share is replaced
for the duration of each test. The checkouts are D:\\0_SourceCode\\msdial_interactive_app and
D:\\0_SourceCode\\msdial_repository_catalog, or MSDIAL_INTERACTIVE_ROOT and MSDIAL_CATALOG_ROOT.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402,F401  (puts scripts/ on the path)
from campaign import plan, policy, ports  # noqa: E402

INTERACTIVE_ROOT = Path(os.environ.get("MSDIAL_INTERACTIVE_ROOT") or r"D:\0_SourceCode\msdial_interactive_app")
CATALOG_ROOT = Path(os.environ.get("MSDIAL_CATALOG_ROOT") or r"D:\0_SourceCode\msdial_repository_catalog")
AVAILABLE = (INTERACTIVE_ROOT / "msdial_app" / "mcp_server.py").is_file() and (
    CATALOG_ROOT / "src" / "msdial_repository_catalog" / "mcp_server.py"
).is_file()
if AVAILABLE:
    for root in (CATALOG_ROOT / "src", INTERACTIVE_ROOT):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

# Every tool the ports call, with the parameters they pass (InteractivePort and CatalogPort).
INTERACTIVE_TOOLS = {
    "msdial_download_repository_raw": {
        "repository", "accession", "workspace_root", "maximum_gb", "raw_retention_policy", "allow_preflight",
        "confirmed", "analysis_unit_handoff_path", "analysis_purpose", "campaign_authorization_path", "host", "port",
    },
    "msdial_interactive_job": {"job_id", "detail", "log_lines", "host", "port"},
    "msdial_cancel_job": {"job_id", "reason", "host", "port"},
    "msdial_repository_raw_metadata_preflight": {"manifest_path", "extractor_path", "max_inputs", "confirm_untargeted", "host", "port"},
    "msdial_split_repository_unit": {"manifest_path", "confirmed", "campaign_authorization_path", "host", "port"},
    "msdial_prepare_repository_reanalysis": {
        "manifest_path", "confirmed", "allow_partial_mapping", "campaign_authorization_path", "host", "port",
    },
    "msdial_start_peak_count_diagnostic": {
        "input_path", "answers", "representative_file", "confirmed", "campaign_authorization_path",
        "timeout_seconds", "idle_timeout_seconds", "host", "port",
    },
    "msdial_estimate_peak_height": {
        "job_id", "target_peak_count_min", "target_peak_count_max", "threshold_step", "manifest_path", "host", "port",
    },
    "msdial_prepare_guided_analysis": {"input_path", "answers", "host", "port"},
    "msdial_start_guided_analysis": {
        "input_path", "answers", "confirmed", "campaign_authorization_path", "timeout_seconds",
        "idle_timeout_seconds", "host", "port",
    },
    "msdial_generate_lcms_qa": {"manifest_path", "host", "port"},
    "msdial_generate_publication_report": {"manifest_path", "run_qa", "host", "port"},
    "msdial_interactive_create_handoff": {"job_id", "host", "port"},
    "msdial_cleanup_repository_raw": {"manifest_path", "confirmed", "campaign_authorization_path", "host", "port"},
}
CATALOG_TOOLS = {
    "msdial_catalog_save_class_proposal": {
        "unit_id", "purpose", "selected_fields", "assignments_json", "rationale", "confirmed", "database", "abstain",
        "ratification",
    },
    "msdial_catalog_reanalysis_handoff": {"unit_id", "class_proposal_id", "database"},
    "msdial_catalog_record_analysis_run": {
        "run_id", "unit_id", "status", "class_proposal_id", "interactive_version", "msdial_version", "mztab_path",
        "mztab_sha256", "gate_verdict", "gate_exit_code", "provenance", "database",
    },
}
REPOSITORY_FUNCTIONS = {
    "read_manifest": {"path"},
    "load_unit_manifest": {"manifest_path"},
    "record_campaign_authorization": {"manifest_path", "crossing"},
    "discard_download_lease": {"manifest_path", "confirmed"},
    "live_run_attempt": {"manifest_path"},
    "lease_owner_state": {"manifest"},
}


@unittest.skipUnless(AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class InteractiveContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from msdial_app import mcp_server, repository_reanalysis

        cls.tools = mcp_server
        cls.rr = repository_reanalysis

    def test_a_decorated_tool_is_the_function_itself(self) -> None:
        def probe(value: int = 1) -> int:
            return value

        self.assertIs(self.tools.mcp.tool()(probe), probe, "MCPServer().tool() must hand the function back")
        for name in INTERACTIVE_TOOLS:
            self.assertTrue(inspect.isfunction(getattr(self.tools, name)), name)

    def test_every_parameter_the_port_passes_is_taken(self) -> None:
        for name, wanted in INTERACTIVE_TOOLS.items():
            with self.subTest(name):
                accepted = set(inspect.signature(getattr(self.tools, name)).parameters)
                self.assertEqual(sorted(wanted - accepted), [])
        for name, wanted in REPOSITORY_FUNCTIONS.items():
            with self.subTest(name):
                accepted = set(inspect.signature(getattr(self.rr, name)).parameters)
                self.assertEqual(sorted(wanted - accepted), [])
        from msdial_app import process_liveness, raw_metadata_extractor, workflow

        self.assertTrue(callable(process_liveness.process_is_alive))
        self.assertTrue(callable(process_liveness.process_created_at))
        self.assertTrue(callable(raw_metadata_extractor.inspect_raw_metadata_extractor))
        self.assertTrue(callable(workflow.console_assembly_path))

    def request_raising(self, error: BaseException):
        return mock.patch.object(self.tools, "_request_json", side_effect=error)

    def test_refusals_come_back_in_the_shape_the_policy_reads(self) -> None:
        from msdial_app.campaign_authorization import CampaignAuthorizationError

        port = ports.InteractivePort(port=8766)
        answers = {"workflow_overrides": {}}
        cases = [
            (self.tools.MsdialRequestError("busy", status=409, payload={"code": "unit_busy", "live_job_id": "J1"}),
             policy.BUSY, {"reason": "unit_busy", "live_job_id": "J1"}),
            (self.tools.MsdialRequestError("Could not connect"), policy.FAULT, {"reason": "backend_unavailable"}),
            (self.tools.MsdialRequestError("campaign_authorization_refused [unit_not_covered]: no.", status=400),
             policy.REFUSED, {"reason": "campaign_authorization_refused", "codes": ["unit_not_covered"]}),
            (CampaignAuthorizationError(["revoked"], ["revoked"]), policy.REFUSED, {"codes": ["revoked"]}),
            (ValueError("bad plan"), policy.FAILED, {"reason": "validation_error"}),
            (RuntimeError("escaped"), policy.FAILED, {"reason": "exception", "error_type": "RuntimeError"}),
        ]
        for error, kind, shape in cases:
            with self.subTest(type(error).__name__, kind=kind), self.request_raising(error):
                result = port.start_run(input_path="x.csv", answers=answers, authorization_path="a.json",
                                        timeout_seconds=0, idle_timeout_seconds=60)
                self.assertIs(result.get("ok"), False)
                self.assertEqual(policy.classify_result(result), kind)
                for key, value in shape.items():
                    self.assertEqual(result.get(key), value, key)

    def test_the_calls_carry_the_campaign_authorization_never_confirmed_true(self) -> None:
        port = ports.InteractivePort(port=8766)
        seen = []

        def record(method, path, **options):
            seen.append((method, path, options))
            return {"started": True, "job_id": "J9"}

        with mock.patch.object(self.tools, "_request_json", side_effect=record):
            started = port.start_run(input_path="x.csv", answers={}, authorization_path="auth.json",
                                     timeout_seconds=0, idle_timeout_seconds=21600)
            port.start_diagnostic(input_path="x.csv", answers={}, authorization_path="auth.json",
                                  timeout_seconds=0, idle_timeout_seconds=10800)
            port.cancel("J9", "stalled")
        self.assertEqual(started, {"started": True, "job_id": "J9"})
        run, diagnostic, cancel = seen
        self.assertEqual((run[1], run[2]["port"]), ("/api/agent/run", 8766))
        self.assertEqual(run[2]["body"]["campaign_authorization_path"], "auth.json")
        self.assertIs(run[2]["body"]["confirmed"], False)
        self.assertEqual((run[2]["body"]["timeout_seconds"], run[2]["body"]["idle_timeout_seconds"]), (0.0, 21600.0))
        self.assertEqual(diagnostic[1], "/api/agent/tuning/run")
        self.assertIs(diagnostic[2]["body"]["confirmed"], False)
        self.assertEqual(cancel[1], "/api/jobs/J9/cancel")

    def test_a_job_the_backend_no_longer_holds_is_job_not_found(self) -> None:
        port = ports.InteractivePort(port=8766)
        with self.request_raising(self.tools.MsdialRequestError("Job not found", status=404)):
            self.assertEqual(port.job("gone")["reason"], "job_not_found")
        with self.request_raising(self.tools.MsdialRequestError("Could not connect")):
            self.assertEqual(policy.classify_result(port.job("any")), policy.FAULT)

    def test_an_in_process_tool_reports_rather_than_raises(self) -> None:
        port = ports.InteractivePort(port=8766)
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "no" / "run-manifest.json")
            for result in (
                port.preflight(manifest_path=missing, extractor_path="x.exe", authorization_path=""),
                port.prepare_metadata(manifest_path=missing, authorization_path=""),
                port.download(repository="metabolights", accession="MTBLS1", workspace_root=directory, maximum_gb=1,
                              raw_retention_policy="keep", handoff_path=str(Path(directory) / "none.json"),
                              analysis_purpose="p", authorization_path=""),
            ):
                self.assertIs(result.get("ok"), False, result)
                self.assertEqual(policy.classify_result(result), policy.FAILED)
        self.assertIsNone(port.read_manifest(missing))
        self.assertEqual(port.lease_state({"lease_owner": {"pid": 2**31 - 7, "process_created_at": 1.0}}), "gone")

    def test_a_missing_tool_is_unsupported_not_a_crash(self) -> None:
        port = ports.InteractivePort(port=8766)
        result = port._call("msdial_no_such_tool", job_id="x")
        self.assertEqual((result["ok"], result["reason"]), (False, "unsupported"))
        result = port._call("msdial_cancel_job", job_id="x", no_such_parameter=1)
        self.assertEqual(result["reason"], "unsupported")
        capabilities = port.capabilities()
        self.assertEqual(set(capabilities), {"version", "classify_preflight", "authorized_discard", "split_parent_release", "cancel_job"})
        self.assertTrue(capabilities["cancel_job"])

    def test_the_authorization_record_the_runner_writes_is_the_one_interactive_accepts(self) -> None:
        from msdial_app.campaign_authorization import CampaignAuthorization, CampaignAuthorizationError

        with tempfile.TemporaryDirectory() as directory:
            manifest = {
                "campaign_id": "c1", "raw_retention_policy": "delete_after_validated_output",
                "units": [{"unit_id": "u1"}, {"unit_id": "u2"}],
                "pins": {"libraries": [{"name": "Positive.msp", "sha256": "a" * 64, "bytes": 5}]},
            }
            manifest_path = Path(directory) / "campaign-manifest.json"
            digest = plan.write_manifest(manifest_path, manifest)
            record = plan.authorization_record(
                manifest, manifest_path, digest, approval_id="A-2026-10-01", approved_by="Test Person",
                approved_at="2026-10-01T00:00:00+00:00", statement="Approved.", covers=["1", "3", "4", "5", "split"],
            )
            path = Path(directory) / "campaign-authorization.json"
            ports.write_json_atomic(path, record)
            authorization = CampaignAuthorization.load(path)
            for boundary in (1, 3, 4, 5, "split"):
                self.assertTrue(authorization.check("u1", boundary)["valid"], boundary)
            self.assertTrue(authorization.check("u1-DDA", 4, parent_unit_id="u1")["valid"], "a derived part is covered")
            self.assertFalse(authorization.check("u3", 1)["valid"])
            self.assertFalse(authorization.check("u1", 6)["valid"])
            self.assertNotIn("path", json.dumps(record["libraries"]))
            manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
            with self.assertRaises(CampaignAuthorizationError):
                CampaignAuthorization.load(path)


@unittest.skipUnless(AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class CatalogContractTests(unittest.TestCase):
    def test_every_parameter_the_port_passes_is_taken(self) -> None:
        from msdial_repository_catalog import campaign_lock, class_selection
        from msdial_repository_catalog import mcp_server as tools
        from msdial_repository_catalog.storage import Catalog

        for name, wanted in CATALOG_TOOLS.items():
            with self.subTest(name):
                function = getattr(tools, name)
                self.assertEqual(sorted(wanted - set(inspect.signature(function).parameters)), [])
        self.assertEqual(sorted({"database", "approval_id", "campaign_id"} - set(inspect.signature(campaign_lock.acquire_campaign_lock).parameters)), [])
        self.assertTrue(callable(campaign_lock.release_campaign_lock) and callable(campaign_lock.campaign_lock_status))
        self.assertTrue(callable(class_selection.automatic_class_proposal) and callable(class_selection.abstention_record))
        self.assertTrue(callable(Catalog.download_plan))
        self.assertTrue(hasattr(tools, "DEFAULT_DATABASE"))

    def test_the_ratification_the_runner_passes_is_accepted(self) -> None:
        from msdial_repository_catalog.ratification import normalize_ratification, ratification_record

        ratification = {
            "approval_id": "A-2026-10-01", "manifest_digest": "sha256:" + "b" * 64, "authorization_sha256": "c" * 64,
            "campaign_id": "c1", "proposal_id": "p1",
        }
        self.assertEqual(normalize_ratification(ratification)["approval_id"], "A-2026-10-01")
        self.assertEqual(ratification_record(ratification, "p1", abstention=True)["proposal_id"], "p1")
        with self.assertRaises(ValueError):
            ratification_record(ratification, "another-proposal", abstention=True)

    def test_the_run_record_the_runner_writes_is_accepted(self) -> None:
        from msdial_repository_catalog.storage import Catalog

        from msdial_repository_catalog import mcp_server as tools

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite"
            with Catalog(database) as catalog:
                catalog.initialize()
            result = tools.msdial_catalog_record_analysis_run(
                run_id="u1:job", unit_id="u1", status="outputs_produced", mztab_path=r"D:\analysis\u1\output\x.mztab",
                database=str(database),
            )
            self.assertFalse(result.get("recorded"), "an absolute location is refused, so the runner records relative paths")
            self.assertIn("relative", result["message"])
            # The shape CatalogPort.record_run reads: recorded, or a message.
            result = tools.msdial_catalog_record_analysis_run(
                run_id="u1:job", unit_id="u1", status="outputs_produced", mztab_path="output/x.mztab",
                gate_verdict=policy.gate_verdict_token(4), gate_exit_code=4, database=str(database),
            )
            self.assertEqual((result.get("recorded"), "Unknown analysis unit" in result.get("message", "")), (False, True))


class PlanContractTests(unittest.TestCase):
    def test_the_manifest_digest_is_the_hash_of_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.json"
            digest = plan.write_manifest(path, {"b": 1, "a": [1, "é"]})
            self.assertEqual(digest, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(path.read_bytes(), policy.canonical_json({"a": [1, "é"], "b": 1}))


if __name__ == "__main__":
    unittest.main()
