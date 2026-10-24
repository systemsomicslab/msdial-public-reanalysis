"""The workspace root is the campaign's, named every time, and the backend on the campaign port must serve it.

Until 2026-10-24 every runner command without --workspace-root fell back to D:\\13_MSDIAL_Public_Reanalysis\\analysis
without a word. Production now runs on a root of its own and that folder keeps the pilots and preflights, so a
command aimed at the production root looked on D:, said the campaign had no approval, or planned against the
pilots' workspaces. The runner now takes --workspace-root, else MSDIAL_REPOSITORY_WORKSPACE_ROOT, and refuses
with neither; the backend check compares the root the listener on the campaign port was started for with the
campaign's (its environment, else the campaign's launch record), so a backend left running for another
campaign's root is refused; setup-windows.ps1 refuses a root on C:, in the user profile, in a temporary folder or
inside a code checkout instead of any root off D:.

The process tests start a stand-in for Interactive's app.py (test_campaign_backend_lifetime.FAKE_APP) on scratch
ports 8790, 8795 and 8796, never Interactive itself, and stop every process they started.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402,F401  (puts scripts/ on the path)
import test_campaign_backend_lifetime as lifetime  # noqa: E402
import test_campaign_contract as contract  # noqa: E402  (puts the Interactive checkout on the path)
from campaign import ports  # noqa: E402

VARIABLE = "MSDIAL_REPOSITORY_WORKSPACE_ROOT"
_SPEC = importlib.util.spec_from_file_location("campaign_runner_cli_root", TESTS.parent / "scripts" / "campaign-runner.py")
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)


def _without_variable() -> contextlib.AbstractContextManager:
    environment = {key: value for key, value in os.environ.items() if key.upper() != VARIABLE}
    return mock.patch.dict(os.environ, environment, clear=True)


def _as_interactive(test: unittest.TestCase, version: str) -> None:
    """The Interactive the runner imported reports `version`: the real package's attribute where it imports, else a
    stand-in module in sys.modules for the test alone (patch.dict of sys.modules would also drop every module the
    test imported meanwhile)."""
    try:
        import msdial_app
    except ImportError:
        msdial_app = types.ModuleType("msdial_app")
        sys.modules["msdial_app"] = msdial_app
        test.addCleanup(sys.modules.pop, "msdial_app", None)
    patcher = mock.patch.object(msdial_app, "__version__", version, create=True)
    patcher.start()
    test.addCleanup(patcher.stop)


def _main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class RunnerRootTests(unittest.TestCase):
    """--workspace-root, else MSDIAL_REPOSITORY_WORKSPACE_ROOT, else a refusal: no root is assumed."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="workspace-root-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.option_root = self.directory / "option-root"
        self.variable_root = self.directory / "variable-root"

    def test_the_option_comes_first_then_the_variable_and_a_blank_value_names_none(self) -> None:
        resolve = cli.resolve_workspace_root
        option, variable = str(self.option_root), str(self.variable_root)
        self.assertEqual(resolve(option, {VARIABLE: variable}), self.option_root)
        self.assertEqual(resolve(None, {VARIABLE: f" {variable} "}), self.variable_root)
        self.assertEqual(resolve("  ", {VARIABLE: variable}), self.variable_root, "a blank option names no root")
        self.assertEqual((cli.workspace_root_source(option), cli.workspace_root_source("  "), cli.workspace_root_source(None)),
                         ("--workspace-root", VARIABLE, VARIABLE))
        for environment in ({}, {VARIABLE: ""}, {VARIABLE: "   "}):
            with self.subTest(environment=environment), self.assertRaises(cli.NoWorkspaceRoot) as raised:
                resolve(None, environment)
            self.assertIn("--workspace-root", str(raised.exception))
            self.assertIn(VARIABLE, str(raised.exception))
        self.assertEqual(cli.WORKSPACE_ROOT_VARIABLE, ports.WORKSPACE_ROOT_VARIABLE)

    def test_a_relative_root_or_one_inside_a_checkout_is_refused_whichever_names_it(self) -> None:
        """Review of 2026-10-25: a relative root was taken and read against the working directory (the code checkout,
        for a scheduled task), plan and approve wrote there, and every unit then failed at its download on
        Interactive's "must be an absolute path"; a root inside a checkout was taken too."""
        checkout = self.directory / "code"
        (checkout / ".git").mkdir(parents=True)
        worktree = self.directory / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="ascii")
        cases = {"analysis": "is not an absolute path", "F:": "is not an absolute path",
                 "F:analysis": "is not an absolute path", "\\analysis": "is not an absolute path",
                 str(checkout / "analysis"): f"is inside the code checkout {checkout}",
                 str(checkout): f"is inside the code checkout {checkout}",
                 str(worktree / "data" / "analysis"): f"is inside the code checkout {worktree}"}
        for root, problem in cases.items():
            for option, environment, source in ((root, {VARIABLE: str(self.variable_root)}, "--workspace-root"),
                                                (None, {VARIABLE: root}, VARIABLE)):
                with self.subTest(root=root, source=source), self.assertRaises(cli.NoWorkspaceRoot) as raised:
                    cli.resolve_workspace_root(option, environment)
                self.assertIn(f"The workspace root {Path(root)}, which {source} names, {problem}", str(raised.exception))
                self.assertIn("No campaign file was read or written", str(raised.exception))
        self.assertEqual(cli.resolve_workspace_root(str(self.directory / "codebase" / "analysis"), {}),
                         self.directory / "codebase" / "analysis", "a folder named like a checkout is not inside it")

    def test_a_command_given_a_relative_root_is_refused_and_writes_nothing_where_it_runs(self) -> None:
        working = self.directory / "working"
        working.mkdir()
        previous = os.getcwd()
        os.chdir(working)
        self.addCleanup(os.chdir, previous)
        for argv in (["--workspace-root", "analysis", "status", "--campaign", "c1"],
                     ["--workspace-root", "analysis", "approve", "--campaign", "c1", "--digest", "sha256:x",
                      "--approval-id", "a", "--by", "b", "--statement", "s", "--covers", "1"]):
            with self.subTest(command=argv[2]), _without_variable(), \
                    mock.patch.object(cli, "_approved_ledger", side_effect=AssertionError("a ledger was looked for")):
                code, out, err = _main(argv)
            self.assertEqual((code, out), (cli.EXIT_REFUSED, ""))
            self.assertIn("The workspace root analysis, which --workspace-root names, is not an absolute path", err)
        with _without_variable():
            os.environ[VARIABLE] = "analysis"
            code, _out, err = _main(["status", "--campaign", "c1"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn(f"which {VARIABLE} names, is not an absolute path", err)
        self.assertEqual(list(working.iterdir()), [], "nothing was written where the command ran")

    def test_a_command_with_no_root_named_is_refused_before_it_reads_or_writes_anything(self) -> None:
        for argv in (["status", "--campaign", "c1"], ["verify-env", "--campaign", "c1"],
                     ["schedule-command", "--campaign", "c1", "--xml-out", str(self.directory / "task.xml")],
                     ["approve", "--campaign", "c1", "--digest", "sha256:x", "--approval-id", "a", "--by", "b",
                      "--statement", "s", "--covers", "1"]):
            with self.subTest(command=argv[0]), _without_variable(), \
                    mock.patch.object(cli, "_approved_ledger", side_effect=AssertionError("a ledger was looked for")):
                code, out, err = _main(argv)
            self.assertEqual(code, cli.EXIT_REFUSED)
            self.assertEqual(out, "")
            self.assertIn("No workspace root is named", err)
            self.assertIn("never assumed", err)
            self.assertNotIn("_campaigns", err, "no campaign directory was looked at")
        self.assertEqual(list(self.directory.iterdir()), [], "nothing was written")

    def test_the_variable_names_the_root_without_the_option_and_the_option_wins_over_it(self) -> None:
        with mock.patch.dict(os.environ, {VARIABLE: str(self.variable_root)}):
            code, _out, err = _main(["status", "--campaign", "c1"])
            self.assertEqual(code, cli.EXIT_REFUSED)
            self.assertIn(str(self.variable_root / "_campaigns" / "c1" / "ledger.sqlite"), err)
            code, _out, err = _main(["--workspace-root", str(self.option_root), "status", "--campaign", "c1"])
            self.assertEqual(code, cli.EXIT_REFUSED)
            self.assertIn(str(self.option_root / "_campaigns" / "c1" / "ledger.sqlite"), err)
            self.assertNotIn(str(self.variable_root), err)
        self.assertFalse(self.variable_root.exists() or self.option_root.exists(), "an unapproved campaign makes nothing")

    def test_the_task_definition_names_the_root_even_where_the_variable_gave_it(self) -> None:
        target = self.directory / "task.xml"
        with mock.patch.dict(os.environ, {VARIABLE: str(self.variable_root)}):
            code, out, _err = _main(["schedule-command", "--campaign", "c1", "--xml-out", str(target)])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("schtasks /Create", out)
        arguments = target.read_text(encoding="utf-16")
        self.assertIn(f'--workspace-root "{self.variable_root.resolve()}"', arguments)
        self.assertIn(f'"{self.variable_root.resolve() / "_campaigns" / "c1" / "logs" / "runner.log"}"', arguments)

    def test_while_a_command_runs_the_variable_is_the_campaigns_root_and_is_put_back(self) -> None:
        seen: list[str | None] = []

        def status(_args) -> int:
            seen.append(os.environ.get(VARIABLE))
            return cli.EXIT_OK

        def unapproved(_args) -> int:
            seen.append(os.environ.get(VARIABLE))
            raise cli.Unapproved("not approved")

        for before in (None, str(self.variable_root)):
            for handler, expected in ((status, cli.EXIT_OK), (unapproved, cli.EXIT_REFUSED)):
                with self.subTest(before=before, handler=handler.__name__), _without_variable():
                    if before is not None:
                        os.environ[VARIABLE] = before
                    seen.clear()
                    with mock.patch.object(cli, "command_status", side_effect=handler):
                        code, _out, _err = _main(["--workspace-root", str(self.option_root), "status", "--campaign", "c1"])
                    self.assertEqual(code, expected)
                    self.assertEqual(seen, [str(self.option_root)], "the command ran bounded to the campaign's root")
                    self.assertEqual(os.environ.get(VARIABLE), before, "the user's value is put back")

    @unittest.skipUnless(contract.AVAILABLE, "needs the Interactive and Catalog checkouts")
    def test_the_in_process_interactive_tools_are_bounded_to_the_campaigns_root_not_the_users(self) -> None:
        """Interactive's MCP boundary reads the variable at each call; the runner calls those tools in-process, so a
        user's variable naming the production root would refuse a campaign on the archive root, and an unset one
        bounds nothing."""
        try:
            from msdial_app import mcp_server
        except ImportError as error:
            self.skipTest(f"Interactive's MCP server cannot be imported here: {error}")
        archive, production = self.directory / "archive", self.directory / "production"
        with _without_variable():
            os.environ[VARIABLE] = str(production)
            with self.assertRaises(ValueError):
                mcp_server._validated_workspace_root(str(archive))
            with cli.bounded_to(archive):
                self.assertEqual(Path(mcp_server._validated_workspace_root(str(archive))), archive.resolve())
                with self.assertRaises(ValueError) as raised:
                    mcp_server._validated_workspace_root(str(production))
                self.assertIn("boundary", str(raised.exception))
            self.assertEqual(os.environ[VARIABLE], str(production))


class BackendRootCheckTests(unittest.TestCase):
    """check() compares the root the listener was started for with the campaign's, as the process says it."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="backend-root-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.interactive = self.directory / "interactive"
        self.interactive.mkdir()
        self.root = self.directory / "production"
        self.other = self.directory / "archive"
        _as_interactive(self, "x")
        self.config = {"app_version": "x", "root": str(self.interactive)}

    def supervisor(self) -> ports.BackendSupervisor:
        return ports.BackendSupervisor(
            python=sys.executable, interactive_root=self.interactive, host="127.0.0.1", port=8799,
            jobs_file=self.directory / "campaign" / "backend" / "agent-jobs.json", workspace_root=str(self.root),
            launch_method="child")

    def check(self, *, listener: int | None, environment: dict | None, record: dict | None = None,
              owns: bool | None = False) -> list[str]:
        supervisor = self.supervisor()
        if record is not None:
            supervisor.launch_record_path.parent.mkdir(parents=True, exist_ok=True)
            supervisor.launch_record_path.write_text(json.dumps(record))
        with mock.patch.object(ports, "listening_pid", return_value=listener), \
                mock.patch.object(ports, "process_environment", return_value=environment), \
                mock.patch.object(ports.BackendSupervisor, "_owns", return_value=owns):
            return supervisor.check(self.config)

    def test_a_backend_started_for_the_campaigns_root_passes_however_the_root_is_spelled(self) -> None:
        upper = str(self.root).upper() if os.name == "nt" else str(self.root)
        for spelled in (str(self.root), str(self.root) + "\\", upper):
            with self.subTest(spelled=spelled):
                self.assertEqual(self.check(listener=4242, environment={VARIABLE: spelled}), [])
        if os.name == "nt":
            self.assertEqual(self.check(listener=4242, environment={VARIABLE.lower(): str(self.root)}), [],
                             "Windows reads a variable whatever its case, and so does the check")

    def test_a_backend_started_for_another_root_is_refused_and_both_roots_are_named(self) -> None:
        problems = self.check(listener=4242, environment={VARIABLE: str(self.other)})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("another workspace root", problems[0])
        self.assertIn(str(self.other), problems[0])
        self.assertIn("the environment of pid 4242", problems[0])
        self.assertIn(f"this campaign's is {self.root}", problems[0])

    def test_a_backend_started_for_no_root_is_refused(self) -> None:
        problems = self.check(listener=4242, environment={"PATH": "x"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("started for no workspace root", problems[0])
        self.assertIn(VARIABLE, problems[0])

    def test_where_the_environment_cannot_be_read_the_launch_record_says_it_only_for_its_own_backend(self) -> None:
        own = {"method": "wmi", "pid": 4242, "workspace_root": str(self.root)}
        self.assertEqual(self.check(listener=4242, environment=None, record=own), [])
        moved = {**own, "workspace_root": str(self.other)}
        problems = self.check(listener=4242, environment=None, record=moved)
        self.assertIn("another workspace root", problems[0])
        self.assertIn("the campaign's launch record", problems[0])
        # A listener the record does not name, nor trace to, is not said to serve the record's root.
        problems = self.check(listener=5151, environment=None, record=own, owns=False)
        self.assertIn("cannot be read", problems[0])
        self.assertEqual(self.check(listener=5151, environment=None, record=own, owns=True), [], "a launcher's child")
        # psutil cannot say which process listens: the record is all there is, as for origin().
        self.assertEqual(self.check(listener=None, environment=None, record=own), [])
        # A record written before 2026-10-24 keeps no root, and no record at all says nothing.
        self.assertIn("cannot be read", self.check(listener=4242, environment=None, record={"pid": 4242})[0])
        self.assertIn("cannot be read", self.check(listener=None, environment=None)[0])

    def test_the_launch_record_keeps_the_root_the_backend_was_started_for(self) -> None:
        supervisor = self.supervisor()
        with mock.patch.object(supervisor, "_launch_child", return_value={"method": "child", "pid": 4242}):
            supervisor._launch()
        record = json.loads(supervisor.launch_record_path.read_text())
        self.assertEqual(record["workspace_root"], str(self.root))
        self.assertEqual(supervisor.environment({})[VARIABLE], str(self.root))
        # A start that may have happened is recorded with its root as well.
        supervisor.launch_method = "wmi"
        with mock.patch.object(os, "name", "nt"), \
                mock.patch.object(supervisor, "_launch_wmi", side_effect=ports.BackendLaunchError(
                    "no report", may_have_started=True, record={"method": "wmi", "pid": None, "broker_pid": 1})):
            with self.assertRaises(ports.BackendLaunchError):
                supervisor._launch()
        self.assertEqual(json.loads(supervisor.launch_record_path.read_text())["workspace_root"], str(self.root))

    def test_a_refusal_on_a_reused_backend_makes_it_unusable_and_says_why(self) -> None:
        supervisor = self.supervisor()
        with mock.patch.object(supervisor, "status", return_value={"app_version": "x"}), \
                mock.patch.object(supervisor, "read_config", return_value=(self.config, "")), \
                mock.patch.object(supervisor, "origin", return_value={"pid": 4242, "how": "unknown"}), \
                mock.patch.object(ports, "listening_pid", return_value=4242), \
                mock.patch.object(ports, "process_environment", return_value={VARIABLE: str(self.other)}):
            result = supervisor.ensure()
        self.assertFalse(result["ok"])
        self.assertFalse(result["started"])
        self.assertIn("another workspace root", result["detail"])


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("psutil"), "Windows processes read through psutil")
class BackendRootProcessTests(unittest.TestCase):
    """The real check against real processes: the stand-in's environment is what says its root."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="backend-root-process-"))
        self.fake = lifetime.FakeInteractive(self.directory)
        self.started: list[int] = []
        self.processes: list[subprocess.Popen] = []
        _as_interactive(self, "fake-1.0")

    def tearDown(self) -> None:
        for pid in self.started:
            lifetime._kill_tree(pid)
        for process in self.processes:
            process.wait(timeout=30)
        shutil.rmtree(self.directory, ignore_errors=True)

    def supervisor(self, port: int, root: Path) -> ports.BackendSupervisor:
        return ports.BackendSupervisor(
            python=sys.executable, interactive_root=self.fake.root, host="127.0.0.1", port=port,
            jobs_file=self.fake.jobs_file, workspace_root=str(root), launch_method="child", start_timeout=60,
            config_timeout=30)

    def stand_in(self, port: int, root: Path | None) -> int:
        self.assertTrue(lifetime._port_free(port), f"scratch port {port} is in use")
        environment = {key: value for key, value in os.environ.items() if key.upper() != VARIABLE}
        if root is not None:
            environment[VARIABLE] = str(root)
        process = subprocess.Popen([sys.executable, str(self.fake.root / "app.py"), "--host", "127.0.0.1", "--port",
                                    str(port), "--no-browser"],
                                   env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        self.started.append(process.pid)
        self.processes.append(process)
        deadline = time.monotonic() + 30
        while lifetime._get(port, "/api/agent/status?limit=0", timeout=2) is None:
            self.assertLess(time.monotonic(), deadline, "the stand-in did not answer")
            time.sleep(0.2)
        return process.pid

    def test_a_backend_left_running_for_another_root_is_refused_and_one_for_this_root_is_reused(self) -> None:
        archive, production = self.directory / "archive", self.directory / "production"
        pid = self.stand_in(8795, archive)
        refused = self.supervisor(8795, production).ensure()
        self.assertFalse(refused["ok"], refused)
        self.assertFalse(refused["started"], "nothing is started beside it")
        self.assertIn(f"another workspace root ({archive}, as the environment of pid {pid} says", refused["detail"])
        reused = self.supervisor(8795, archive).ensure()
        self.assertTrue(reused["ok"], reused)
        self.assertFalse(reused["started"])

    def test_a_backend_started_for_no_root_is_refused(self) -> None:
        self.stand_in(8796, None)
        refused = self.supervisor(8796, self.directory / "production").ensure()
        self.assertFalse(refused["ok"], refused)
        self.assertIn("started for no workspace root", refused["detail"])

    def test_the_backend_the_runner_starts_serves_the_campaigns_root(self) -> None:
        port, production = 8790, self.directory / "production"
        self.assertTrue(lifetime._port_free(port), f"scratch port {port} is in use")
        supervisor = self.supervisor(port, production)
        started = supervisor.ensure()
        listener = ports.listening_pid(port)
        for pid in (started.get("pid"), listener):
            if pid:
                self.started.append(int(pid))
        if supervisor._child is not None:
            self.processes.append(supervisor._child)
        self.assertTrue(started["ok"], started)
        self.assertTrue(started["started"])
        self.assertEqual(supervisor.served_workspace_root(), (str(production), f"the environment of pid {listener}"))
        self.assertEqual(json.loads(supervisor.launch_record_path.read_text())["workspace_root"], str(production))
        refused = self.supervisor(port, self.directory / "archive").ensure()
        self.assertFalse(refused["ok"], "another campaign's runner does not take it over")
        self.assertIn("another workspace root", refused["detail"])


# The harness: Get-WorkspaceRootProblem (and Test-WithinFolder) taken from setup-windows.ps1 by its syntax tree,
# so nothing else of the script runs (it installs packages), and called once per case.
HARNESS = textwrap.dedent(r'''
    $ErrorActionPreference = "Stop"
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SETUP_SCRIPT, [ref]$null, [ref]$null)
    $definition = [System.Management.Automation.Language.FunctionDefinitionAst]
    foreach ($function in $ast.FindAll({ param($node) $node -is $definition }, $false)) {
        Invoke-Expression $function.Extent.Text
    }
    foreach ($case in (Get-Content -Raw -LiteralPath $env:SETUP_CASES | ConvertFrom-Json)) {
        $parameters = @{ Root = [string]$case.root }
        foreach ($name in @("SystemDrives", "Profiles", "TempFolders", "Checkouts")) {
            if ($null -ne $case.$name) { $parameters[$name] = [string[]]@($case.$name) }
        }
        Write-Output ("RESULT:" + (Get-WorkspaceRootProblem @parameters))
    }
''')


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell")
class SetupRootTests(unittest.TestCase):
    """setup-windows.ps1 refuses a root on C:, in the profile, in a temporary folder or in a checkout, not one off D:."""

    def problems(self, cases: list[dict]) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                 base64.b64encode(HARNESS.encode("utf-16-le")).decode("ascii")],
                env={**os.environ, "SETUP_SCRIPT": str(TESTS.parent / "setup-windows.ps1"), "SETUP_CASES": str(path)},
                capture_output=True, text=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return [line[len("RESULT:"):] for line in completed.stdout.splitlines() if line.startswith("RESULT:")]

    def test_the_root_is_refused_on_c_in_the_profile_in_temp_and_in_a_checkout_and_taken_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Resolved: Windows PowerShell's GetFullPath spells a short (8.3) name out in full, and so does resolve().
            base = Path(directory).resolve()
            for folder in (base, *base.parents):
                if (folder / ".git").exists():
                    self.skipTest(f"the scratch folder is inside a checkout ({folder})")
            (base / "repo" / ".git").mkdir(parents=True)
            (base / "worktree").mkdir()
            (base / "worktree" / ".git").write_text("gitdir: elsewhere\n", encoding="ascii")
            # The scratch folder stands in for a data volume: the system drive, profile and temp are named apart.
            elsewhere = {"SystemDrives": ["Q:"], "Profiles": [str(base / "profile")], "TempFolders": [str(base / "tmp")],
                         "Checkouts": [str(base / "code")]}
            cases = [
                {"root": str(base / "analysis"), **elsewhere},
                {"root": str(base / "analysis") + "\\", **elsewhere},
                {"root": "Q:\\MSDIAL\\analysis", **elsewhere},
                {"root": "q:/MSDIAL/analysis", **elsewhere},
                {"root": str(base / "profile" / "analysis"), **elsewhere},
                {"root": str(base / "TMP" / "x" / "analysis"), **elsewhere},
                {"root": str(base / "code" / "analysis"), **elsewhere},
                {"root": str(base / "code"), **elsewhere},
                {"root": str(base / "repo" / "data" / "analysis"), **elsewhere},
                {"root": str(base / "worktree" / "analysis"), **elsewhere},
                {"root": str(base / "codebase" / "analysis"), **elsewhere},
                {"root": "analysis", **elsewhere},
                {"root": "", **elsewhere},
                {"root": "C:\\MSDIAL\\analysis"},
                {"root": str(Path(os.environ["USERPROFILE"]) / "analysis"), "SystemDrives": ["Q:"]},
            ]
            problems = self.problems(cases)
        self.assertEqual(len(problems), len(cases), problems)
        self.assertEqual(problems[0:2], ["", ""], "a folder off the system drive, profile, temp and checkouts is taken")
        self.assertEqual(problems[2:4], ["it is on Q:", "it is on q:"])
        self.assertTrue(problems[4].startswith("it is inside the user profile"), problems[4])
        self.assertTrue(problems[5].startswith("it is inside the temporary folder"), problems[5])
        self.assertEqual(problems[6:8], [f"it is inside the code checkout {base / 'code'}"] * 2)
        self.assertEqual(problems[8], f"it is inside the code checkout {base / 'repo'}", "found by its .git folder")
        self.assertEqual(problems[9], f"it is inside the code checkout {base / 'worktree'}", "a worktree's .git file")
        self.assertEqual(problems[10], "", "a folder whose name begins like a checkout's is not inside it")
        self.assertEqual(problems[11], "it is not an absolute path")
        self.assertTrue(problems[12].startswith("no root was given: pass -ReanalysisRoot"), problems[12])
        self.assertEqual(problems[13], "it is on C:", "C: is refused by default")
        self.assertTrue(problems[14].startswith("it is inside the user profile"), problems[14])

    def test_a_network_or_device_spelling_of_a_drive_and_a_drive_not_mounted_are_refused(self) -> None:
        """Review of 2026-10-25: \\\\localhost\\C$\\... named a folder on C:, in the profile too, in a spelling the
        drive-letter and prefix checks never saw, and was taken; \\\\?\\C:\\... stopped on an unrelated error, and a
        drive that is not mounted on Join-Path's "Cannot find drive" at the .git walk."""
        profile = Path(os.environ["USERPROFILE"])
        inside_profile = "\\\\localhost\\C$\\" + str(profile.relative_to(profile.anchor)) + "\\analysis"
        absent = next((letter for letter in "ZYXWVUTSRPONMLKJIHGE" if not os.path.exists(f"{letter}:\\")), None)
        cases = [{"root": root} for root in (
            "\\\\localhost\\C$\\MSDIAL\\analysis", "\\\\127.0.0.1\\c$\\x", inside_profile,
            f"\\\\{os.environ.get('COMPUTERNAME', 'localhost')}\\C$\\x", "\\\\?\\UNC\\localhost\\C$\\x", "\\\\?\\C:\\x",
            "\\\\.\\C:\\x", "//localhost/C$/x", "\\\\nas\\share\\analysis")]
        if absent is not None:
            # Off the system drive, the profile and temp as the other test names them, so only the drive is left.
            cases.append({"root": f"{absent}:\\MSDIAL\\analysis", "SystemDrives": ["Q:"], "Profiles": [],
                          "TempFolders": []})
        problems = self.problems(cases)
        self.assertEqual(len(problems), len(cases), problems)
        for case, problem in zip(cases[:9], problems[:9]):
            self.assertEqual(problem, "it is a network or device path; give a folder on a drive of this PC, as X:\\folder",
                             case["root"])
        if absent is not None:
            self.assertEqual(problems[9], f"its drive {absent}: is not mounted on this PC")


# The whole script, as README tells a person to run it, with a stand-in interpreter that records any call and fails:
# a refusal must come before the root's folder is made and before anything is installed.
FAKE_PYTHON = "@echo %* >> \"%~dp0called.txt\"\r\n@exit /b 1\r\n"


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell")
class SetupScriptTests(unittest.TestCase):
    """setup-windows.ps1 itself: -ReanalysisRoot has no default, and a refused root stops the script (review of
    2026-10-25: SetupRootTests calls the function alone, so restoring the D: default or dropping the throw passed)."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="setup-script-")).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.python = self.directory / "tools" / "python.cmd"
        self.python.parent.mkdir()
        self.python.write_text(FAKE_PYTHON, encoding="ascii", newline="")
        for name in ("interactive", "catalog"):
            (self.directory / name).mkdir()

    def setup(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(TESTS.parent / "setup-windows.ps1"), "-Python", str(self.python),
             "-InteractiveRoot", str(self.directory / "interactive"), "-CatalogRoot", str(self.directory / "catalog"),
             *arguments],
            capture_output=True, text=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)

    def assert_refused(self, completed: subprocess.CompletedProcess, root: str, problem: str) -> None:
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        said = " ".join((completed.stderr + completed.stdout).split())
        self.assertIn(f"The repository reanalysis workspace root cannot be '{root}': {problem}", said)
        self.assertNotIn("Repository reanalysis workspace:", completed.stdout, "no folder was made")
        self.assertFalse((self.python.parent / "called.txt").exists(), "nothing was installed")

    def test_without_a_root_the_script_stops_before_it_makes_or_installs_anything(self) -> None:
        self.assert_refused(self.setup(), "", "no root was given: pass -ReanalysisRoot")

    def test_a_refused_root_stops_the_script_and_its_folder_is_not_made(self) -> None:
        root = self.directory / "analysis"
        system = root.drive.casefold() in ("c:", os.environ.get("SystemDrive", "C:").casefold())
        self.assert_refused(self.setup("-ReanalysisRoot", str(root)), str(root),
                            f"it is on {root.drive}" if system else "it is inside the temporary folder")
        self.assertFalse(root.exists())
        shared = "\\\\localhost\\C$\\" + str(root.relative_to(root.anchor))
        self.assert_refused(self.setup("-ReanalysisRoot", shared), shared, "it is a network or device path")
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
