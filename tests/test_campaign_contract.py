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

import contextlib
import functools
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
    "msdial_repository_raw_metadata_preflight": {
        "manifest_path", "extractor_path", "max_inputs", "confirm_untargeted", "campaign_authorization_path", "host", "port",
    },
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
    "classify_preflight": {"manifest_path", "campaign_authorization_path"},
    "disposition_hold": {"manifest"},
}


def campaign_unit(root: Path, status: str = "download_failed", units: tuple[str, ...] = ("u1",)) -> dict:
    """A campaign authorization covering u1 and a unit workspace of u1 with raw data, as a runner leaves them."""
    campaign_manifest = root / "campaign-manifest.json"
    campaign_manifest.write_bytes(b'{"campaign_id":"c1"}')
    authorization = root / "campaign-authorization.json"
    authorization.write_text(json.dumps({
        "schema": plan.AUTHORIZATION_SCHEMA, "approval_id": "A1", "campaign_id": "c1",
        "manifest_digest": plan.digest_of(campaign_manifest.read_bytes()),
        "campaign_manifest_path": str(campaign_manifest), "approved_by": "Test Person",
        "approved_at": "2026-10-01T00:00:00+00:00", "statement": "Approved.", "covers": [1, 3, 4, 5, "split"],
        "units": list(units), "raw_retention_policy": "delete_after_validated_output", "libraries": [], "revoked_at": None,
    }), encoding="utf-8")
    workspace = root / "analysis" / "metabolights" / "MTBLS1" / "u1"
    raw = workspace / "raw" / "data" / "S1.mzML"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x" * 1000)
    (workspace / "output").mkdir()
    (workspace / "provenance").mkdir()
    manifest_path = workspace / "provenance" / "run-manifest.json"
    manifest_path.write_text(json.dumps({
        "status": status, "project": {"analysis_unit_id": "u1"}, "workspace": str(workspace),
        "raw_directory": str(workspace / "raw"), "output_directory": str(workspace / "output"),
        "raw_retention_policy": "delete_after_validated_output", "cleanup_allowed": False,
    }), encoding="utf-8")
    return {"authorization": authorization, "workspace": workspace, "raw": raw, "manifest_path": manifest_path}


def boundary_5_crossings(manifest_path: Path) -> list:
    recorded = json.loads(manifest_path.read_text(encoding="utf-8")).get("campaign_authorizations") or []
    return [item for item in recorded if str(item.get("boundary")) == "5"]


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
        self.assertEqual(set(capabilities), {
            "version", "classify_preflight", "preflight_authorization", "disposition_hold", "authorized_cleanup",
            "authorized_discard", "release_disposition_hold", "split_parent_release", "cancel_job", "lease_uses_store",
        })
        # What the runner cannot run without, since Interactive 0.5.17 (verify-env refuses otherwise).
        for name in ("cancel_job", "classify_preflight", "preflight_authorization", "disposition_hold", "authorized_cleanup"):
            self.assertTrue(capabilities[name], name)

    def test_a_discard_interactive_would_refuse_records_no_crossing(self) -> None:
        """Interactive refuses to discard raw data beside an mzTab-M. The port's fallback finds that out
        before it records a boundary-5 crossing; a failed download is discarded under one crossing."""
        port = ports.InteractivePort(port=8766)
        if port.capabilities()["authorized_discard"]:
            self.skipTest("Interactive has its own authorized discard; the port's fallback is not used")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_manifest = root / "campaign-manifest.json"
            campaign_manifest.write_bytes(b'{"campaign_id":"c1"}')
            authorization = root / "campaign-authorization.json"
            authorization.write_text(json.dumps({
                "schema": plan.AUTHORIZATION_SCHEMA, "approval_id": "A1", "campaign_id": "c1",
                "manifest_digest": plan.digest_of(campaign_manifest.read_bytes()),
                "campaign_manifest_path": str(campaign_manifest), "approved_by": "Test Person",
                "approved_at": "2026-10-01T00:00:00+00:00", "statement": "Approved.", "covers": [1, 3, 4, 5, "split"],
                "units": ["u1"], "raw_retention_policy": "delete_after_validated_output", "libraries": [], "revoked_at": None,
            }), encoding="utf-8")
            workspace = root / "analysis" / "metabolights" / "MTBLS1" / "u1"
            raw = workspace / "raw" / "data" / "S1.mzML"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"x" * 1000)
            (workspace / "output").mkdir()
            (workspace / "provenance").mkdir()
            manifest_path = workspace / "provenance" / "run-manifest.json"
            mztab = workspace / "output" / "u1.mzTab"
            mztab.write_text("MTD\tmzTab-version\t2.0.0-M\n", encoding="utf-8")

            def write(status: str) -> None:
                manifest_path.write_text(json.dumps({
                    "status": status, "project": {"analysis_unit_id": "u1"}, "workspace": str(workspace),
                    "raw_directory": str(workspace / "raw"), "output_directory": str(workspace / "output"),
                    "raw_retention_policy": "delete_after_validated_output", "cleanup_allowed": False,
                }), encoding="utf-8")

            def crossings() -> list:
                recorded = json.loads(manifest_path.read_text(encoding="utf-8")).get("campaign_authorizations") or []
                return [item for item in recorded if str(item.get("boundary")) == "5"]

            for status, codes in (("validation_failed", ["mztab_output_exists"]),
                                  ("mztab_validated", ["validated_status", "mztab_output_exists"])):
                with self.subTest(status):
                    write(status)
                    for _ in range(3):  # the first try and both retries
                        result = port.discard(manifest_path=str(manifest_path), authorization_path=str(authorization), unit_id="u1")
                        self.assertEqual((result["ok"], result["deleted"], result["blockers"]), (True, False, codes))
                    self.assertTrue(policy.discard_blocked_for_good(result["blockers"]))
                    self.assertEqual(crossings(), [], "no crossing for a deletion that did not happen")
                    self.assertTrue(raw.is_file())
            mztab.unlink()
            write("download_failed")
            result = port.discard(manifest_path=str(manifest_path), authorization_path=str(authorization), unit_id="u1")
            self.assertTrue(result["deleted"], result)
            self.assertEqual(len(crossings()), 1)
            self.assertFalse((workspace / "raw").exists())

    def test_no_discard_runs_under_a_console_that_may_still_read_the_raw_tree(self) -> None:
        """A Console a backend restart left running is found by Interactive's live_run_attempt (this test's
        own process stands in for it). Neither the fallback nor an approval-taking discard deletes under it,
        and no crossing is recorded; once its run attempt is closed, the discard proceeds."""
        from msdial_app.process_liveness import process_created_at

        port = ports.InteractivePort(port=8766)
        for authorized in (False, True):
            with self.subTest(authorized=authorized), tempfile.TemporaryDirectory() as directory:
                unit = campaign_unit(Path(directory))
                manifest = json.loads(unit["manifest_path"].read_text(encoding="utf-8"))
                manifest["run_attempts"] = [{"attempt_id": "a1", "kind": "run", "job_id": "rn1", "ended_at": None,
                                             "console_pid": os.getpid(), "console_process_created_at": process_created_at(),
                                             "backend": {"pid": 0}}]
                unit["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")
                calls = []

                def discard_download_lease(manifest_path, confirmed=False, campaign_authorization_path="", **_entry):
                    calls.append(str(manifest_path))
                    return {"deleted": True, "raw_directory": "raw"}

                patch = (mock.patch.object(self.rr, "discard_download_lease", discard_download_lease) if authorized
                         else contextlib.nullcontext())
                with patch:
                    result = port.discard(manifest_path=str(unit["manifest_path"]), authorization_path=str(unit["authorization"]),
                                          unit_id="u1")
                    self.assertEqual((result["ok"], result["deleted"], result["blockers"]), (True, False, ["console_live"]))
                    self.assertFalse(policy.discard_blocked_for_good(result["blockers"]), "the Console ends")
                    self.assertEqual((calls, boundary_5_crossings(unit["manifest_path"])), ([], []))
                    self.assertTrue(unit["raw"].is_file())
                    manifest["run_attempts"][0].update(ended_at="2026-10-01T01:00:00+00:00", console_pid=0)
                    unit["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")
                    result = port.discard(manifest_path=str(unit["manifest_path"]), authorization_path=str(unit["authorization"]),
                                          unit_id="u1")
                self.assertTrue(result["deleted"], result)

    def test_a_failed_leases_record_tells_a_short_disk_from_the_network(self) -> None:
        """The download_failure a lease writes into the unit manifest as it fails (_record_download_failure)
        is what the machine adds to a failed job's detail (Runner._lease_failure, only for that job's lease)
        and what the policy reads: a volume that ran short while an archive expanded is a short disk with the
        expansion it declared, a stalled or lost transfer is the network's. The job's own text, str(error),
        says the same for the backend's job record, which carries nothing else."""
        from types import SimpleNamespace

        from campaign import machine
        from msdial_app import archives

        port = ports.InteractivePort(port=8766)
        reader = SimpleNamespace(_manifest=lambda unit: port.read_manifest(unit["manifest_path"]))
        short = archives.ArchiveError(
            "insufficient_disk_space", "S.zip expands to 5000 bytes; 900 are free and 100 are held in reserve.",
            detail={"declared_bytes": 5000, "free_bytes": 900},
        )
        cases = (
            (short, "extract", 5000, False),
            (archives.ArchiveError("insufficient_disk_space", "Free space fell below the 100-byte reserve while S.zip expanded."),
             "extract", 0, False),
            (self.rr.download_interruption(TimeoutError("timed out"), 300), "fetch", None, True),
            (self.rr.download_interruption(ConnectionResetError(10054, "reset by peer"), 300), "fetch", None, True),
            (archives.ArchiveError("archive_member_escapes", "S.zip names a member outside its folder."), "extract", None, False),
        )
        for error, stage, expands, network in cases:
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as directory:
                manifest_path = Path(directory) / "run-manifest.json"
                lease = {"status": "downloading", "project": {"files": []}, "lease_owner": self.rr._new_lease_owner("dl1")}
                self.rr._record_download_failure(manifest_path, lease, [], error, stage=stage)
                unit = {"manifest_path": str(manifest_path)}
                failure = machine.Runner._lease_failure(reader, unit, "dl1")
                self.assertEqual(failure["error_type"], type(error).__name__)
                self.assertEqual(machine.Runner._lease_failure(reader, unit, "dl2"), {}, "another job's lease")
                detail = {"job_id": "dl1", "status": "failed", "error": str(error), "stop_reason": None, "failure": failure}
                self.assertEqual(policy.extraction_disk_short(detail), expands)
                self.assertEqual(policy.network_failure(detail), network)
                job_only = {"job_id": "dl1", "status": "failed", "error": str(error), "stop_reason": None}
                self.assertEqual(policy.network_failure(job_only), network)
                self.assertEqual(policy.extraction_disk_short(job_only) is None, expands is None)

    def test_the_default_extractor_is_the_newest_built_pin(self) -> None:
        import msdial_app
        from msdial_app import raw_metadata_extractor as extractor

        built = next(item for item in extractor.PINNED_BUILDS if item["state"] == extractor.PIN_BUILT)
        path = ports.default_extractor_path(Path(r"D:/0_SourceCode/msdial_interactive_app"))
        # A pin may name the folder it was built in (Interactive 0.5.35); otherwise both commits name it.
        folder = built.get("build_folder") or (
            f"RawMetadataExtractor-{built[extractor.RAW_TREE][:9]}-{built[extractor.COMMON_TREE][:9]}")
        self.assertEqual(path.parts[-7:], (
            folder, "msrawdataworkbench",
            "RawMetadataConsoleApp", "bin", "Release", "net48", "RawMetadataConsoleApp.exe"))
        version = tuple(int(part) for part in msdial_app.__version__.split(".")[:3])
        if version >= (0, 5, 35):
            self.assertTrue(built[extractor.RAW_TREE].startswith("5f604462d"), "the pin of Interactive 0.5.35")
            self.assertEqual("RawMetadataExtractor-5f60446-f0583493a", folder, "where the 0.5.35 pin was built")
        elif version >= (0, 5, 19):
            self.assertTrue(built[extractor.RAW_TREE].startswith("a12293c61"), "the pin of Interactive 0.5.19")

    def test_the_preflight_is_given_the_approval(self) -> None:
        port = ports.InteractivePort(port=8766)
        seen = {}
        real = self.tools.msdial_repository_raw_metadata_preflight

        @functools.wraps(real)  # the port reads the real signature
        def preflight(**arguments):
            seen.update(arguments)
            return {"completed": True, "extractor_found": True}

        with mock.patch.object(self.tools, "msdial_repository_raw_metadata_preflight", preflight):
            port.preflight(manifest_path="m.json", extractor_path="x.exe", authorization_path="auth.json")
        self.assertEqual((seen["campaign_authorization_path"], seen["max_inputs"], seen["confirm_untargeted"]),
                         ("auth.json", 0, False))

    def test_classify_preflight_holds_a_unit_with_no_preflight_and_refuses_another_units_approval(self) -> None:
        port = ports.InteractivePort(port=8766)
        with tempfile.TemporaryDirectory() as directory:
            unit = campaign_unit(Path(directory), status="raw_metadata_required")
            result = port.classify(manifest_path=str(unit["manifest_path"]), authorization_path=str(unit["authorization"]))
            self.assertEqual((result["ok"], result["applied"], result["held"]["reason"]),
                             (True, False, "raw_metadata_preflight_missing"))
            self.assertNotIn("campaign_disposition", json.loads(unit["manifest_path"].read_text(encoding="utf-8")),
                             "a held unit is left as it is")
        with tempfile.TemporaryDirectory() as directory:
            unit = campaign_unit(Path(directory), units=("u2",))
            result = port.classify(manifest_path=str(unit["manifest_path"]), authorization_path=str(unit["authorization"]))
            self.assertEqual(policy.classify_result(result), policy.REFUSED)

    def test_an_approval_taking_discard_is_used_the_day_it_exists(self) -> None:
        """Plan item 14's discard is found by its signature, and it checks and records the approval itself:
        the port then records no crossing of its own. Its refusals are read as the fallback's codes."""
        port = ports.InteractivePort(port=8766)
        calls = []

        def discard_download_lease(manifest_path, confirmed=False, campaign_authorization_path="", **_entry):
            calls.append((str(manifest_path), campaign_authorization_path))
            if "refuse" in str(manifest_path):
                # Interactive 0.5.22: a refusal under an approval is returned as blockers, nothing deleted.
                return {"deleted": False, "confirmation_required": False,
                        "blockers": ["mzTab-M output exists; finalize the run before deleting raw data."]}
            return {"deleted": True, "raw_directory": "raw"}

        with tempfile.TemporaryDirectory() as directory:
            unit = campaign_unit(Path(directory))
            with mock.patch.object(self.rr, "discard_download_lease", discard_download_lease), \
                    mock.patch.object(port.ca, "authorize", side_effect=AssertionError("the port authorizes nothing itself")):
                self.assertTrue(port.capabilities()["authorized_discard"])
                result = port.discard(manifest_path=str(unit["manifest_path"]), authorization_path="auth.json", unit_id="u1")
                self.assertEqual((result["ok"], result["deleted"]), (True, True))
                # A unit whose manifest exists, as the MCP tool resolves it before it discards anything.
                refuse_root = Path(directory) / "refuse"
                refuse_root.mkdir()
                refuse_unit = campaign_unit(refuse_root)
                refused = port.discard(manifest_path=str(refuse_unit["manifest_path"]), authorization_path="auth.json", unit_id="u1")
            self.assertEqual((refused["deleted"], refused["blockers"]), (False, ["mztab_output_exists"]))
            self.assertEqual(calls[0], (str(unit["manifest_path"]), "auth.json"))
            self.assertEqual(boundary_5_crossings(unit["manifest_path"]), [])

    def test_a_disposition_hold_is_released_only_when_the_discard_is_told_to(self) -> None:
        """The agreed contract of 2026-10-07: Interactive's discard and split-parent release take
        release_disposition_hold, default false, and never discard a held unit without it. The port passes it
        only when asked (an operator's skip of a held unit), reads the refusal as disposition_held, and an
        Interactive that cannot take it is unsupported, never a discard made without it."""
        from msdial_app import mcp_server

        port = ports.InteractivePort(port=8766)
        calls: list = []
        refusal = ("The unit's campaign disposition holds it (aif_multi_ce_awaiting_console): it is to run once a "
                   "Console that can exists, and a held unit's raw data are kept.")

        def with_release(download_job_id="", manifest_path="", confirmed=False, host="", port=0,
                         campaign_authorization_path="", release_disposition_hold=False):
            calls.append(("discard", release_disposition_hold))
            if not release_disposition_hold:
                return {"deleted": False, "confirmation_required": False, "blockers": [refusal]}
            return {"deleted": True, "raw_directory": "raw"}

        def without_release(download_job_id="", manifest_path="", confirmed=False, host="", port=0,
                            campaign_authorization_path=""):
            calls.append(("discard", "no argument"))
            return {"deleted": False, "confirmation_required": False, "blockers": [refusal]}

        def release_with(manifest_path, campaign_authorization_path=None, release_disposition_hold=False, **_entry):
            calls.append(("release", release_disposition_hold))
            return {"deleted": bool(release_disposition_hold)}

        def release_without(manifest_path, campaign_authorization_path=None, **_entry):
            calls.append(("release", "no argument"))
            return {"deleted": False}

        with tempfile.TemporaryDirectory() as directory:
            unit = campaign_unit(Path(directory))
            arguments = {"manifest_path": str(unit["manifest_path"]), "authorization_path": str(unit["authorization"]),
                         "unit_id": "u1"}
            with mock.patch.object(mcp_server, "msdial_discard_repository_raw", with_release), \
                    mock.patch.object(self.rr, "cleanup_split_parent", release_with, create=True):
                self.assertTrue(port.capabilities()["release_disposition_hold"])
                held = port.discard(**arguments)
                self.assertEqual((held["ok"], held["deleted"], held["blockers"]), (True, False, ["disposition_held"]))
                self.assertTrue(policy.discard_blocked_for_good(held["blockers"]), "waiting does not lift a hold")
                released = port.discard(**arguments, release_disposition_hold=True)
                self.assertEqual((released["ok"], released["deleted"]), (True, True))
                self.assertFalse(port.release_split_parent(manifest_path=arguments["manifest_path"],
                                                           authorization_path="auth.json")["deleted"])
                self.assertTrue(port.release_split_parent(manifest_path=arguments["manifest_path"],
                                                          authorization_path="auth.json",
                                                          release_disposition_hold=True)["deleted"])
            self.assertEqual(calls, [("discard", False), ("discard", True), ("release", False), ("release", True)])
            calls.clear()
            with mock.patch.object(mcp_server, "msdial_discard_repository_raw", without_release), \
                    mock.patch.object(self.rr, "cleanup_split_parent", release_without, create=True):
                self.assertFalse(port.capabilities()["release_disposition_hold"])
                result = port.discard(**arguments, release_disposition_hold=True)
                self.assertEqual((result["ok"], result["reason"]), (False, "unsupported"))
                self.assertEqual(policy.classify_result(result), policy.CONTRACT)
                result = port.release_split_parent(manifest_path=arguments["manifest_path"],
                                                   authorization_path="auth.json", release_disposition_hold=True)
                self.assertEqual((result["ok"], result["reason"]), (False, "unsupported"))
                self.assertEqual(port.discard(**arguments)["blockers"], ["disposition_held"])
            self.assertEqual(calls, [("discard", "no argument")], "nothing is called with a release it cannot take")
            self.assertTrue(unit["raw"].is_file())

    def test_the_fallback_moves_the_containers_out_before_it_discards(self) -> None:
        """A finalisation hold (MS-DIAL's containers still in the raw tree) is retried first, as Interactive's
        cleanup does; the discard follows only once no hold stands, and a hold that stands records nothing."""
        from msdial_app import run_finalisation

        port = ports.InteractivePort(port=8766)
        if port.capabilities()["authorized_discard"]:
            self.skipTest("Interactive has its own authorized discard; the port's fallback is not used")
        for resolves in (True, False):
            with self.subTest(resolves=resolves), tempfile.TemporaryDirectory() as directory:
                unit = campaign_unit(Path(directory))
                manifest = json.loads(unit["manifest_path"].read_text(encoding="utf-8"))
                manifest[run_finalisation.HOLDS] = [{"id": "h1", "blocks": [run_finalisation.BLOCKS_RAW_DELETION],
                                                     "step": "relocate_intermediates", "job_id": "rn1", "reason": "locked"}]
                unit["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")

                def resolve(path, log=None):
                    if resolves:  # Interactive moved the containers and cleared its hold
                        current = json.loads(Path(path).read_text(encoding="utf-8"))
                        current[run_finalisation.HOLDS] = []
                        Path(path).write_text(json.dumps(current), encoding="utf-8")
                        return []
                    return list(manifest[run_finalisation.HOLDS])

                with mock.patch.object(run_finalisation, "resolve_finalisation_holds", side_effect=resolve) as retried:
                    result = port.discard(manifest_path=str(unit["manifest_path"]), authorization_path=str(unit["authorization"]),
                                          unit_id="u1")
                self.assertEqual(retried.call_count, 1)
                if resolves:
                    self.assertTrue(result["deleted"], result)
                    self.assertEqual(len(boundary_5_crossings(unit["manifest_path"])), 1)
                else:
                    self.assertEqual((result["deleted"], result["blockers"]), (False, ["finalisation_held"]))
                    self.assertFalse(policy.discard_blocked_for_good(result["blockers"]), "a hold may yet be resolved")
                    self.assertEqual(boundary_5_crossings(unit["manifest_path"]), [])
                    self.assertTrue(unit["raw"].is_file())
                self.assertTrue((unit["workspace"] / "output").is_dir(), "the output is never touched")

    def test_a_blocked_download_names_the_bytes_interactive_needs(self) -> None:
        port = ports.InteractivePort(port=8766)
        preview = {"blocking_reasons": ["size_limit:exceeded"], "required_download_bytes": 5 * 1000**4, "maximum_gb": 1.0}

        @functools.wraps(self.tools.msdial_download_repository_raw)  # the port reads the real signature
        def blocked(**_arguments):
            return {"started": False, "blocked": True, "preview": preview}

        with mock.patch.object(self.tools, "msdial_download_repository_raw", blocked):
            result = port.download(repository="metabolights", accession="MTBLS1", workspace_root="D:/analysis", maximum_gb=1,
                                   raw_retention_policy="keep", handoff_path="h.json", analysis_purpose="p", authorization_path="")
        self.assertEqual((result["reason"], result["required_download_bytes"]), ("blocked", 5 * 1000**4))

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

    def test_the_runs_the_runner_records_are_accepted_with_their_class_proposal(self) -> None:
        """Every status the runner sends for a production attempt and for a unit's end, with the unit's saved
        Class decision, for the unit and for a split part of it (<unit>-<part>, recorded under the unit)."""
        import sqlite3

        from msdial_repository_catalog.storage import Catalog

        from campaign import machine

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite"
            with Catalog(database) as catalog:
                catalog.initialize()
            connection = sqlite3.connect(database)
            with connection:
                connection.execute("INSERT INTO study(study_id, repository, accession, source_hash) VALUES ('s1', 'metabolights', 'MTBLS9000', 'h')")
                connection.execute(
                    "INSERT INTO analysis_unit(unit_id, study_id, source_subrecord_id, separation, acquisition_mode, ion_mode, "
                    "ion_mobility, instrument, untargeted, signature, target_omics) "
                    "VALUES ('u1', 's1', 'u1', 'LC-MS', 'DDA', 'Positive', 'Unknown', 'Agilent', NULL, 'u1', 'Metabolomics')"
                )
                for index in range(2):
                    connection.execute("INSERT INTO sample(sample_pk, unit_id, sample_id, raw_file) VALUES (?, 'u1', ?, ?)",
                                       (f"pk{index}", f"S{index}", f"S{index}.mzML"))
                    connection.execute(
                        "INSERT INTO raw_file(file_id, unit_id, path, role, size_bytes, download_url, sample_id) "
                        "VALUES (?, 'u1', ?, 'raw', 10, ?, ?)", (f"f{index}", f"S{index}.mzML", f"https://x/S{index}.mzML", f"S{index}"))
            connection.close()
            port = ports.CatalogPort(database)
            decision = port.class_decision("u1", "annotation")
            saved = port.save_class(unit_id="u1", purpose="annotation", kind=decision["kind"], ratification={
                "approval_id": "approval-1", "manifest_digest": "sha256:" + "0" * 64, "campaign_id": "test-campaign",
                "proposal_id": decision["proposal_id"]})
            self.assertTrue(saved["ok"], saved)
            world = campaign_fakes.World(root / "world", ["u1"])
            world_ports = world.ports()
            world_ports.catalog = port
            with world.open() as book:
                runner = machine.Runner(book, world_ports, resources={"libraries": world.libraries})
                unit = {**book.unit("u1"), "class_proposal_id": saved["proposal_id"], "run_job_id": "rn0003"}
                for job, status in (("rn0001", "timed_out"), ("rn0002", "outputs_not_validated"), ("rn0003", "cancelled"),
                                    ("rn0004", "interrupted"), ("rn0005", "failed"), ("rn0003", "outputs_produced")):
                    runner._record_run({**unit, "run_job_id": job}, status, None, job_id=job, final=False)
                for status, exit_code in (("outputs_produced", 4), ("failed", 2), ("skipped", None), ("excluded", None)):
                    runner._record_run(unit, status, exit_code)
                runner._record_run(unit, "completed", 0)
                part = {**unit, "unit_key": "u1-dda", "role": "split_part", "parent_unit_key": "u1", "run_job_id": "rn0006"}
                runner._record_run(part, "outputs_produced", None, job_id="rn0006", final=False)
                runner._record_run(part, "outputs_produced", 4)
                self.assertEqual(book.events("catalog_run_not_recorded"), [])
            with Catalog(database) as catalog:
                last = catalog.get_analysis_run("u1:rn0003")
                self.assertEqual((last["status"], last["gate_verdict"], last["gate_exit_code"]), ("completed", "pass", 0))
                self.assertEqual(last["class_proposal_id"], saved["proposal_id"])
                self.assertEqual(last["provenance"]["attempt_status"], "outputs_produced")
                self.assertEqual(catalog.get_analysis_run("u1:rn0001")["status"], "timed_out")
                self.assertEqual(catalog.get_analysis_run("u1-dda:rn0006")["gate_verdict"], "held")


class PlanContractTests(unittest.TestCase):
    def test_the_manifest_digest_is_the_hash_of_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.json"
            digest = plan.write_manifest(path, {"b": 1, "a": [1, "é"]})
            self.assertEqual(digest, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(path.read_bytes(), policy.canonical_json({"a": [1, "é"], "b": 1}))


@unittest.skipUnless(os.name == "nt", "Windows creation flags")
class BackendLaunchTests(unittest.TestCase):
    def test_the_backend_has_a_windowless_console_its_children_inherit(self) -> None:
        import subprocess

        flags = ports.backend_creation_flags()
        self.assertFalse(flags & subprocess.DETACHED_PROCESS)
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)


if __name__ == "__main__":
    unittest.main()
