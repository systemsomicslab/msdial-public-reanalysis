"""The campaign backend outlives its launcher, keeps a windowless console, and says how it was started.

Review of PR #30 (2026-10-06): a backend started as the runner's child died with a tree kill of the runner,
and every process started from a Claude session sits in the Claude app's job, which allows no breakaway and
is force-closed when the app updates. BackendSupervisor now starts the backend through WMI (a broker that
Win32_Process.Create starts, which starts the backend and exits).

The Windows tests start a stand-in for Interactive's app.py (a few lines of http.server answering
/api/agent/status, /api/config and /probe) on scratch ports 8790-8799 with a scratch job registry, never
Interactive itself, and stop every process they started. They run the real BackendSupervisor, the real
broker and the real WMI call.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402,F401  (puts scripts/ on the path)
from campaign import ports  # noqa: E402

SCRIPTS = TESTS.parent / "scripts"

# What a process can say of its console and job, as JSON: the console window (0: none, or hidden by
# CREATE_NO_WINDOW), the processes sharing its console, and whether it sits in a job object.
CONSOLE_FACTS = textwrap.dedent('''
    import ctypes, ctypes.wintypes as w, json, os
    def console_facts():
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetConsoleWindow.restype = w.HWND
        in_job = w.BOOL()
        k.IsProcessInJob(w.HANDLE(k.GetCurrentProcess()), None, ctypes.byref(in_job))
        pids = (w.DWORD * 64)()
        count = k.GetConsoleProcessList(pids, 64)
        return {"pid": os.getpid(), "console_window": k.GetConsoleWindow() or 0,
                "console_pids": list(pids[:count]), "in_job": bool(in_job.value)}
''')

# The stand-in for Interactive's app.py: --host, --port and --no-browser as the supervisor passes them.
FAKE_APP = CONSOLE_FACTS + textwrap.dedent('''
    import http.server, subprocess, sys, time
    host, port = sys.argv[sys.argv.index("--host") + 1], int(sys.argv[sys.argv.index("--port") + 1])
    ROOT = os.path.dirname(os.path.abspath(__file__))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/agent/status"):
                return self._json({"app_version": "fake-1.0", "jobs": []})
            if self.path == "/api/config":
                time.sleep(float(os.environ.get("FAKE_CONFIG_DELAY") or 0))
                return self._json({"app_version": "fake-1.0", "root": ROOT})
            if self.path == "/probe":
                # A console program started with no creation flags, as Interactive starts git and the Console.
                child = subprocess.run(
                    [sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=60)
                return self._json({"backend": console_facts(), "child": json.loads(child.stdout),
                                   "jobs_file": os.environ.get("MSDIAL_INTERACTIVE_JOBS_FILE")})
            self.send_response(404)
            self.end_headers()

    CHILD = SOURCE + "\\nprint(json.dumps(console_facts()))"
    time.sleep(float(os.environ.get("FAKE_START_DELAY") or 0))
    http.server.ThreadingHTTPServer((host, port), Handler).serve_forever()
''')
FAKE_APP = FAKE_APP.replace("CHILD = SOURCE", "CHILD = " + repr(CONSOLE_FACTS))

# A runner stand-in: it runs the real BackendSupervisor.ensure(), writes what it returned, and waits to be killed.
RUNNER_STUB = textwrap.dedent('''
    import json, sys, time
    sys.dont_write_bytecode = True
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from campaign import ports

    class Supervisor(ports.BackendSupervisor):
        def check(self, config):
            return [] if config.get("app_version") == "fake-1.0" else ["not the stand-in"]

    spec = json.loads(Path(sys.argv[2]).read_text())
    supervisor = Supervisor(python=sys.executable, interactive_root=Path(spec["root"]), host="127.0.0.1",
                            port=spec["port"], jobs_file=Path(spec["jobs_file"]), workspace_root=spec["root"],
                            launch_method=spec["method"], start_timeout=60, config_timeout=30)
    result = supervisor.ensure()
    Path(spec["out"]).write_text(json.dumps(result))
    time.sleep(600)
''')


def _get(port: int, path: str, timeout: float = 30) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except OSError:
        return None


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _kill_tree(pid: int | None) -> None:
    if pid:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)


class FakeInteractive:
    """A scratch Interactive root holding the stand-in app.py, and a scratch job registry."""

    def __init__(self, directory: Path) -> None:
        self.root = directory / "interactive"
        self.root.mkdir()
        (self.root / "app.py").write_text(FAKE_APP, encoding="utf-8")
        self.jobs_file = directory / "campaign" / "backend" / "agent-jobs.json"


@unittest.skipUnless(os.name == "nt", "Windows process trees, consoles and job objects")
class BackendLifetimeTests(unittest.TestCase):
    """Run against the real broker and WMI; each test takes its own scratch port."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="backend-lifetime-"))
        self.fake = FakeInteractive(self.directory)
        self.started: list[int] = []
        self.processes: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for pid in self.started:
            _kill_tree(pid)
        for process in self.processes:
            process.wait(timeout=30)
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)

    def _run_stub(self, port: int, method: str) -> tuple[subprocess.Popen, dict]:
        self.assertTrue(_port_free(port), f"scratch port {port} is in use")
        out = self.directory / f"ensure-{port}.json"
        spec = self.directory / f"stub-{port}.json"
        spec.write_text(json.dumps({"root": str(self.fake.root), "port": port, "jobs_file": str(self.fake.jobs_file),
                                    "method": method, "out": str(out)}))
        stub = subprocess.Popen([sys.executable, "-c", RUNNER_STUB, str(SCRIPTS), str(spec)],
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.started.append(stub.pid)
        self.processes.append(stub)
        deadline = time.monotonic() + 120
        while not out.is_file():
            self.assertIsNone(stub.poll(), "the runner stub ended before ensure() returned")
            self.assertLess(time.monotonic(), deadline, "ensure() did not return within 120 s")
            time.sleep(0.2)
        result = json.loads(out.read_text())
        if result.get("pid"):
            self.started.append(int(result["pid"]))
        return stub, result

    def test_a_backend_started_through_wmi_survives_a_tree_kill_of_the_runner(self) -> None:
        port = 8791
        stub, result = self._run_stub(port, "wmi")
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["started"], result["method"]), (True, "wmi"))
        self.assertIsNotNone(result.get("process_created_at"))
        backend = int(result["pid"])
        record = json.loads((self.fake.jobs_file.parent / "backend-launch.json").read_text())
        self.assertEqual((record["pid"], record["method"]), (backend, "wmi"))
        self.assertEqual(record["process_created_at"], result["process_created_at"])
        self.assertEqual(sorted(path.name for path in self.fake.jobs_file.parent.glob("*.spec.json")), [],
                         "the broker deleted the spec that held the environment")

        subprocess.run(["taskkill", "/T", "/F", "/PID", str(stub.pid)], capture_output=True, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        stub.wait(timeout=30)
        time.sleep(1)
        self.assertIs(ports._process_alive(backend, result["process_created_at"]), True, "the backend died with the runner")
        self.assertIsNotNone(_get(port, "/api/agent/status?limit=0"), "the backend stopped answering")

        # The backend's console: its own, with no window, and outside any job object. A console program it
        # starts with no creation flags shares that console: no window, nothing allocated per call.
        probe = _get(port, "/probe", timeout=60)
        self.assertIsNotNone(probe)
        self.assertEqual(probe["backend"]["pid"], backend)
        self.assertEqual(probe["backend"]["console_window"], 0)
        self.assertFalse(probe["backend"]["in_job"], "the backend sits in a job object")
        self.assertEqual(probe["child"]["console_window"], 0)
        self.assertIn(backend, probe["child"]["console_pids"], "the child did not inherit the backend's console")
        self.assertFalse(probe["child"]["in_job"])
        self.assertEqual(probe["jobs_file"], str(self.fake.jobs_file), "the broker passed the runner's environment")

        # A second runner reuses it and says the first runner started it, through WMI.
        supervisor = self._supervisor(port)
        reused = supervisor.ensure()
        self.assertTrue(reused["ok"], reused)
        self.assertFalse(reused["started"])
        self.assertEqual((reused["origin"]["pid"], reused["origin"]["how"], reused["origin"]["by"]),
                         (backend, "wmi", "this campaign's runner"))
        self.assertTrue(reused.get("origin_new"))
        self.assertNotIn("origin_new", supervisor.ensure(), "a reuse is reported once")

        _kill_tree(backend)
        deadline = time.monotonic() + 20
        while not _port_free(port) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(_port_free(port))

    def test_a_backend_started_as_the_runners_child_dies_with_its_tree(self) -> None:
        """What the WMI start is for: the old way, a child of the runner, ends with a tree kill of the runner."""
        port = 8792
        stub, result = self._run_stub(port, "child")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["method"], "child")
        backend = int(result["pid"])
        probe = _get(port, "/probe", timeout=60)
        self.assertEqual(probe["backend"]["console_window"], 0, "backend_creation_flags gives a windowless console")
        self.assertIn(backend, probe["child"]["console_pids"])
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(stub.pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        stub.wait(timeout=30)
        deadline = time.monotonic() + 20
        while ports._process_alive(backend, result.get("process_created_at")) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(ports._process_alive(backend, result.get("process_created_at")))

    def test_a_backend_started_elsewhere_is_reused_and_said_to_be_of_unknown_origin(self) -> None:
        port = 8793
        self.assertTrue(_port_free(port))
        process = subprocess.Popen([sys.executable, str(self.fake.root / "app.py"), "--host", "127.0.0.1", "--port",
                                    str(port), "--no-browser"], creationflags=subprocess.CREATE_NO_WINDOW)
        self.started.append(process.pid)
        self.processes.append(process)
        deadline = time.monotonic() + 30
        while _get(port, "/api/agent/status?limit=0", timeout=2) is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.2)
        result = self._supervisor(port).ensure()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["started"])
        self.assertEqual((result["origin"]["pid"], result["origin"]["how"]), (process.pid, "unknown"))
        self.assertIn("not started by this campaign's runner", result["origin"]["note"])
        # Started from this test, it shares the test's job, if the test runs in one, and is told so.
        if result["origin"].get("in_job"):
            self.assertIn("job object", result["origin"]["warning"])

    def test_the_creation_flags_give_a_console_children_inherit_without_a_window(self) -> None:
        """Review finding 4: the console inheritance itself, not the flag bits, so that a flag that changes
        console allocation (CREATE_NEW_CONSOLE, DETACHED_PROCESS) fails here."""
        script = CONSOLE_FACTS + textwrap.dedent('''
            import subprocess, sys
            child = subprocess.run([sys.executable, "-c", sys.argv[1] + "\\nprint(json.dumps(console_facts()))"],
                                   capture_output=True, text=True)
            print(json.dumps({"parent": console_facts(), "child": json.loads(child.stdout)}))
        ''')
        completed = subprocess.run([sys.executable, "-c", script, CONSOLE_FACTS], capture_output=True, text=True,
                                   timeout=60, creationflags=ports.backend_creation_flags())
        facts = json.loads(completed.stdout)
        self.assertEqual(facts["parent"]["console_window"], 0)
        self.assertNotEqual(facts["parent"]["console_pids"], [], "the backend has a console")
        self.assertEqual(facts["child"]["console_window"], 0)
        self.assertIn(facts["parent"]["pid"], facts["child"]["console_pids"])

    def _supervisor(self, port: int) -> ports.BackendSupervisor:
        class Supervisor(ports.BackendSupervisor):
            def check(self, config):
                return [] if config.get("app_version") == "fake-1.0" else ["not the stand-in"]

        return Supervisor(python=sys.executable, interactive_root=self.fake.root, host="127.0.0.1", port=port,
                          jobs_file=self.fake.jobs_file, workspace_root=str(self.directory), launch_method="wmi",
                          start_timeout=60, config_timeout=30)


class StartPolicyTests(unittest.TestCase):
    """ensure() without a process: readiness, slow starts and the refusal after repeated failures."""

    def supervisor(self, directory: str, **options) -> ports.BackendSupervisor:
        supervisor = ports.BackendSupervisor(
            python=sys.executable, interactive_root=Path(directory), host="127.0.0.1", port=8799,
            jobs_file=Path(directory) / "backend" / "agent-jobs.json", workspace_root=directory, **options)
        supervisor.check = lambda config: []  # type: ignore[method-assign]
        return supervisor

    def test_three_failed_starts_pause_starting_for_a_while_and_say_why(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, start_timeout=0, retry_after=600)
            clock = {"now": 1000.0}
            launches = []

            def launch():
                launches.append(1)
                raise ports.BackendLaunchError(f"start {len(launches)} failed")

            with mock.patch.object(supervisor, "status", return_value=None), \
                    mock.patch.object(supervisor, "_launch", side_effect=launch), \
                    mock.patch.object(ports.time, "monotonic", side_effect=lambda: clock["now"]):
                for _ in range(3):
                    result = supervisor.ensure()
                self.assertEqual(result["start_failures"], 3)
                refused = supervisor.ensure()
                self.assertEqual(len(launches), 3, "no fourth start inside the pause")
                self.assertIn("could not be started 3 times in a row", refused["detail"])
                for reason in ("start 1 failed", "start 2 failed", "start 3 failed"):
                    self.assertIn(reason, refused["detail"])
                self.assertIn("600 s", refused["detail"])
                clock["now"] += 601
                supervisor.ensure()
                self.assertEqual(len(launches), 4, "starting is tried again after the pause")
                self.assertEqual(supervisor.start_failures, 1)

    def test_a_slow_start_is_waited_for_again_instead_of_started_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, start_timeout=0)
            record = {"method": "wmi", "pid": 4242, "process_created_at": 1.0}
            answers = iter([None, None, None, {"app_version": "x"}, {"app_version": "x"}])
            with mock.patch.object(supervisor, "status", side_effect=lambda: next(answers)), \
                    mock.patch.object(supervisor, "_launch", return_value=record) as launch, \
                    mock.patch.object(supervisor, "_alive", return_value=True), \
                    mock.patch.object(ports, "listening_pid", return_value=4242), \
                    mock.patch.object(supervisor, "config", return_value={"root": directory}):
                first = supervisor.ensure()
                self.assertFalse(first["ok"])
                self.assertIn("left running and waited for again", first["detail"])
                supervisor.start_timeout = 30
                second = supervisor.ensure()
            self.assertEqual(launch.call_count, 1, "the slow backend was not started a second time")
            self.assertTrue(second["ok"], second)
            self.assertEqual((second["pid"], second["method"]), (4242, "wmi"))
            self.assertEqual(supervisor.start_failures, 0)

    def test_readiness_is_the_status_endpoint_and_config_gets_its_own_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, config_timeout=77)
            calls = []

            def get(path, timeout):
                calls.append((path, timeout))
                return {"app_version": "x"}

            with mock.patch.object(supervisor, "_get", side_effect=get):
                supervisor.status()
                supervisor.config()
            self.assertEqual(calls, [("/api/agent/status?limit=0", 5), ("/api/config", 77)])

    def test_a_reused_backend_whose_config_does_not_answer_is_not_usable_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, config_timeout=45)
            with mock.patch.object(supervisor, "status", return_value={"app_version": "x"}), \
                    mock.patch.object(supervisor, "config", return_value=None), \
                    mock.patch.object(ports, "listening_pid", return_value=None):
                result = supervisor.ensure()
            self.assertFalse(result["ok"])
            self.assertIn("did not answer within 45 s", result["detail"])
            self.assertEqual(result["origin"]["how"], "unknown")

    def test_auto_falls_back_to_a_child_and_records_why(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory)
            with mock.patch.object(ports.os, "name", "nt"), \
                    mock.patch.object(supervisor, "_launch_wmi", side_effect=ports.BackendLaunchError("WMI is off")), \
                    mock.patch.object(supervisor, "_launch_child", return_value={"method": "child", "pid": 7}):
                record = supervisor._launch()
            self.assertEqual((record["method"], record["wmi_failure"]), ("child", "WMI is off"))
            self.assertEqual(json.loads(supervisor.launch_record_path.read_text())["wmi_failure"], "WMI is off")
            # A broker that may have started a backend is not followed by a second start.
            with mock.patch.object(ports.os, "name", "nt"), \
                    mock.patch.object(supervisor, "_launch_wmi",
                                      side_effect=ports.BackendLaunchError("no report", may_have_started=True)), \
                    mock.patch.object(supervisor, "_launch_child") as child:
                with self.assertRaises(ports.BackendLaunchError):
                    supervisor._launch()
                child.assert_not_called()

    def test_the_runner_offers_the_same_launch_methods(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("campaign_runner_cli", SCRIPTS / "campaign-runner.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.BACKEND_LAUNCH_METHODS, ports.BACKEND_LAUNCH_METHODS)


class ScheduleDefinitionTests(unittest.TestCase):
    def definition(self) -> ElementTree.Element:
        import importlib.util

        spec = importlib.util.spec_from_file_location("campaign_runner_cli", SCRIPTS / "campaign-runner.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = module.schedule_task_xml(
            python=r"C:\Program Files\Python314\python.exe", script=r"D:\code\scripts\campaign-runner.py", campaign="c1",
            user=r"HOST\a user", workspace_root=r"D:\an analysis", interactive_root=r"D:\i", catalog_root=r"D:\c",
            start="2026-10-06T12:00:00")
        return ElementTree.fromstring(text.split("\n", 1)[1])

    def test_no_time_limit_one_instance_restart_on_failure_and_an_hourly_start(self) -> None:
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        task = self.definition()
        settings = task.find("t:Settings", ns)
        self.assertEqual(settings.findtext("t:ExecutionTimeLimit", namespaces=ns), "PT0S")
        self.assertEqual(settings.findtext("t:MultipleInstancesPolicy", namespaces=ns), "IgnoreNew")
        self.assertEqual(settings.findtext("t:RestartOnFailure/t:Count", namespaces=ns), "3")
        self.assertEqual(settings.findtext("t:DisallowStartIfOnBatteries", namespaces=ns), "false")
        self.assertEqual(task.findtext("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", namespaces=ns), "PT1H")
        self.assertIsNotNone(task.find("t:Triggers/t:LogonTrigger", ns))
        self.assertIsNone(task.find("t:Triggers/t:TimeTrigger/t:ExecutionTimeLimit", ns))
        arguments = task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
        self.assertIn('"D:\\code\\scripts\\campaign-runner.py" --workspace-root "D:\\an analysis"', arguments)
        self.assertIn("run --campaign c1 --until-idle --log-file", arguments)
        self.assertIn(r'"D:\an analysis\_campaigns\c1\logs\runner.log"', arguments)

    def test_the_schedule_command_registers_nothing_and_says_not_to_launch_from_claude(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("campaign_runner_cli", SCRIPTS / "campaign-runner.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "task.xml"
            output = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: output.append(" ".join(map(str, a)))), \
                    mock.patch.object(module.subprocess if hasattr(module, "subprocess") else subprocess, "run") as run:
                code = module.main(["--workspace-root", directory, "schedule-command", "--campaign", "c1",
                                    "--xml-out", str(target)])
            run.assert_not_called()
            self.assertEqual(code, 0)
            text = "\n".join(output)
            self.assertIn(f'schtasks /Create /TN "MSDIAL-campaign-c1" /XML "{target.resolve()}" /F', text)
            self.assertIn("NOT FROM A CLAUDE SESSION", text)
            self.assertIn("ExecutionTimeLimit PT0S", text)
            self.assertTrue(target.read_bytes().startswith(b"\xff\xfe"), "UTF-16 with a byte-order mark")
            self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", target.read_text(encoding="utf-16"))


if __name__ == "__main__":
    unittest.main()
