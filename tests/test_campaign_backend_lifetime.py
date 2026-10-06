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

import contextlib
import importlib.util
import io
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

    def test_a_runner_started_without_a_console_runs_itself_again_in_a_windowless_one(self) -> None:
        """The scheduled task starts the runner with pythonw.exe. Its console children (the gate, the extractor)
        must not each get a console with a window, so it runs itself again under python.exe with CREATE_NO_WINDOW."""
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("no pythonw.exe beside this Python")
        facts = self.directory / "facts.json"
        probe = CONSOLE_FACTS + "\nimport sys\nopen(sys.argv[1], 'w').write(json.dumps(console_facts()))"
        launcher = textwrap.dedent('''
            import importlib.util, sys
            sys.dont_write_bytecode = True
            spec = importlib.util.spec_from_file_location("runner_cli", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert not module._has_console()
            sys.exit(module.run_in_windowless_console([sys.argv[2], "-c", sys.argv[3], sys.argv[4]]))
        ''')
        code = subprocess.run([str(pythonw), "-c", launcher, str(SCRIPTS / "campaign-runner.py"), sys.executable, probe,
                               str(facts)], timeout=60).returncode
        self.assertEqual(code, 0)
        child = json.loads(facts.read_text())
        self.assertEqual(child["console_window"], 0)
        self.assertIn(child["pid"], child["console_pids"], "it has a console of its own")

        # End to end: pythonw.exe campaign-runner.py, as the task runs it, does its work and returns its code.
        target = self.directory / "task.xml"
        code = subprocess.run([str(pythonw), str(SCRIPTS / "campaign-runner.py"), "--workspace-root", str(self.directory),
                               "schedule-command", "--campaign", "c1", "--xml-out", str(target)], timeout=120).returncode
        self.assertEqual(code, 0)
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", target.read_text(encoding="utf-16"))

    def test_a_runner_whose_python_is_a_launcher_knows_its_own_backend_by_the_process_tree(self) -> None:
        """Review of 2026-10-07, finding 1: a venv's python.exe starts the real interpreter as its child, which
        holds the port. The runner refused that backend as another process's, exited with code 3, and the next
        runner called it of unknown origin."""
        port = 8794
        self.assertTrue(_port_free(port))
        venv = self.directory / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=180,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        python = venv / "Scripts" / "python.exe"
        supervisor = self._supervisor(port, python=python)
        result = supervisor.ensure()
        listener = ports.listening_pid(port)
        for pid in (result.get("pid"), result.get("launcher_pid"), listener):
            if pid:
                self.started.append(int(pid))
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["started"], result["method"]), (True, "wmi"))
        self.assertEqual(result["pid"], listener, "the backend is the process that holds the port")
        self.assertTrue(result.get("launcher_pid"), "the venv launcher is recorded")
        self.assertNotEqual(result["launcher_pid"], listener)
        self.assertIs(ports.process_descends_from(listener, result["launcher_pid"]), True)
        record = json.loads(supervisor.launch_record_path.read_text())
        self.assertEqual((record["pid"], record["launcher_pid"]), (listener, result["launcher_pid"]))
        self.assertEqual(supervisor.state.load()["failures"], [])

        # A second runner reuses it as this campaign's own.
        reused = self._supervisor(port, python=python).ensure()
        self.assertTrue(reused["ok"], reused)
        self.assertEqual((reused["origin"]["pid"], reused["origin"]["how"], reused["origin"]["by"]),
                         (listener, "wmi", "this campaign's runner"))
        # A launch record written before 2026-10-07 names the launcher: still this campaign's runner's.
        record.update(pid=record.pop("launcher_pid"), process_created_at=record.pop("launcher_created_at"))
        supervisor.launch_record_path.write_text(json.dumps(record))
        origin = self._supervisor(port, python=python).origin()
        self.assertEqual((origin["how"], origin.get("by"), origin.get("launcher_pid")),
                         ("wmi", "this campaign's runner", record["pid"]))

        _kill_tree(result["launcher_pid"])
        _kill_tree(listener)
        deadline = time.monotonic() + 20
        while not _port_free(port) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(_port_free(port))

    def _supervisor(self, port: int, python: Path | str | None = None) -> ports.BackendSupervisor:
        class Supervisor(ports.BackendSupervisor):
            def check(self, config):
                return [] if config.get("app_version") == "fake-1.0" else ["not the stand-in"]

        return Supervisor(python=str(python or sys.executable), interactive_root=self.fake.root, host="127.0.0.1", port=port,
                          jobs_file=self.fake.jobs_file, workspace_root=str(self.directory), launch_method="wmi",
                          start_timeout=60, config_timeout=30)


@unittest.skipUnless(importlib.util.find_spec("psutil"), "psutil reads parent process ids")
class ProcessTreeTests(unittest.TestCase):
    def test_a_process_descends_from_its_parent_and_grandparent_and_not_the_other_way(self) -> None:
        script = textwrap.dedent('''
            import subprocess, sys
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print(child.pid, flush=True)
            child.wait()
        ''')
        parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        try:
            child = int(parent.stdout.readline())
            created = ports._process_created_at(parent.pid)
            self.assertIs(ports.process_descends_from(child, parent.pid), True)
            self.assertIs(ports.process_descends_from(child, parent.pid, created), True)
            self.assertIs(ports.process_descends_from(child, os.getpid()), True, "through one launcher")
            self.assertIs(ports.process_descends_from(child, os.getpid(), depth=0), False)
            self.assertIs(ports.process_descends_from(parent.pid, child), False)
            self.assertIs(ports.process_descends_from(child, parent.pid, created + 3600), False,
                          "a recorded process created after the listener is a reused process id")
            self.assertIs(ports.process_descends_from(child, child), True)
        finally:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(parent.pid)], capture_output=True) if os.name == "nt" \
                else parent.kill()
            parent.wait(timeout=30)
            parent.stdout.close()


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
                    mock.patch.object(ports.time, "monotonic", side_effect=lambda: clock["now"]), \
                    mock.patch.object(ports.time, "time", side_effect=lambda: clock["now"]):
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

    # ---- /api/config's own deadline (review of 2026-10-07, finding 4) ----

    def _clock(self) -> tuple[dict, contextlib.ExitStack]:
        """A clock that time.sleep advances, for monotonic and wall-clock time alike."""
        clock = {"now": 1_000_000.0}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(ports.time, "monotonic", side_effect=lambda: clock["now"]))
        stack.enter_context(mock.patch.object(ports.time, "time", side_effect=lambda: clock["now"]))
        stack.enter_context(mock.patch.object(ports.time, "sleep", side_effect=lambda s: clock.__setitem__("now", clock["now"] + s)))
        return clock, stack

    def test_config_gets_its_whole_deadline_after_a_status_that_answered_late(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, start_timeout=300, config_timeout=120)
            clock, stack = self._clock()
            begun = clock["now"]
            timeouts = []
            record = {"method": "wmi", "pid": 4242, "process_created_at": 1.0}

            def config(timeout=None):
                timeouts.append(timeout)
                return {"root": directory}

            with stack, mock.patch.object(supervisor, "status",
                                          side_effect=lambda: {"app_version": "x"} if clock["now"] - begun >= 295 else None), \
                    mock.patch.object(supervisor, "_launch", return_value=record), \
                    mock.patch.object(supervisor, "_alive", return_value=True), \
                    mock.patch.object(ports, "listening_pid", return_value=4242), \
                    mock.patch.object(supervisor, "config", side_effect=config):
                result = supervisor.ensure()
            self.assertTrue(result["ok"], result)
            self.assertEqual(timeouts, [120], "not cut to the few seconds left of the start deadline")

    def test_a_backend_that_answers_status_but_not_config_is_reported_as_that_and_asked_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, start_timeout=300, config_timeout=120)
            clock, stack = self._clock()
            calls = []

            def config(timeout=None):
                calls.append(timeout)
                clock["now"] += timeout
                supervisor.last_get_error = "timeout"
                return None

            with stack, mock.patch.object(supervisor, "status", return_value=None), \
                    mock.patch.object(supervisor, "_launch", return_value={"method": "wmi", "pid": 4242}), \
                    mock.patch.object(supervisor, "_alive", return_value=True), \
                    mock.patch.object(ports, "listening_pid", return_value=4242), \
                    mock.patch.object(supervisor, "config", side_effect=config):
                supervisor.status.side_effect = [None, {"app_version": "x"}]
                result = supervisor.ensure()
            self.assertFalse(result["ok"])
            self.assertEqual(calls, [120], "a request that timed out is not sent again over it")
            self.assertIn("answers /api/agent/status, but its /api/config did not answer within 120 s", result["detail"])
            self.assertNotIn("did not answer within 300 s", result["detail"])
            self.assertIsNone(supervisor.state.load()["pending"], "it answers: nothing is waited for as a start")

    def test_a_config_that_fails_at_once_is_asked_again_within_its_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, config_timeout=120)
            clock, stack = self._clock()
            answers = [None, {"root": directory}]

            def config(timeout=None):
                value = answers.pop(0)
                supervisor.last_get_error = None if value else "HTTP 503"
                return value

            with stack, mock.patch.object(supervisor, "config", side_effect=config):
                config_value, why = supervisor.read_config()
            self.assertEqual((config_value, why), ({"root": directory}, ""))
            with stack, mock.patch.object(supervisor, "config", side_effect=lambda timeout=None: (
                    setattr(supervisor, "last_get_error", "HTTP 503"), None)[1]):
                config_value, why = supervisor.read_config()
            self.assertIsNone(config_value)
            self.assertIn("gave no usable answer within 120 s (last: HTTP 503)", why)


class PersistedStartStateTests(unittest.TestCase):
    """Review of 2026-10-07, finding 3: the pause after three failures and the start still waited for lived in one
    runner's memory, and the scheduled task starts a new runner after each one that exits. They are in the ledger."""

    def setUp(self) -> None:
        from campaign import ledger

        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "ledger.sqlite"
        self.books = []
        self.ledger = ledger
        self.clock = {"now": 2_000_000.0}
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(ports.time, "monotonic", side_effect=lambda: self.clock["now"]))
        self.stack.enter_context(mock.patch.object(ports.time, "time", side_effect=lambda: self.clock["now"]))
        self.stack.enter_context(mock.patch.object(
            ports.time, "sleep", side_effect=lambda s: self.clock.__setitem__("now", self.clock["now"] + s)))

    def tearDown(self) -> None:
        self.stack.close()
        for book in self.books:
            book.close()
        self.directory.cleanup()

    def runner(self, **options) -> ports.BackendSupervisor:
        """A supervisor as a new runner process builds it: its own ledger connection, nothing in memory."""
        book = self.ledger.Ledger(self.path, durable=False)
        self.books.append(book)
        supervisor = ports.BackendSupervisor(
            python=sys.executable, interactive_root=Path(self.directory.name), host="127.0.0.1", port=8799,
            jobs_file=Path(self.directory.name) / "backend" / "agent-jobs.json", workspace_root=self.directory.name,
            state=ports.LedgerStartState(book), **options)
        supervisor.check = lambda config: []  # type: ignore[method-assign]
        return supervisor

    def test_the_pause_after_three_failed_starts_holds_for_the_next_runners(self) -> None:
        launches = []

        def launch():
            launches.append(1)
            raise ports.BackendLaunchError(f"start {len(launches)} failed")

        for _ in range(3):
            supervisor = self.runner(start_timeout=0, retry_after=3600)
            with mock.patch.object(supervisor, "status", return_value=None), \
                    mock.patch.object(supervisor, "_launch", side_effect=launch):
                result = supervisor.ensure()
            self.clock["now"] += 300  # the task restarts the runner
        self.assertEqual(result["start_failures"], 3)
        self.assertIn("retry_at", result)
        later = self.runner(start_timeout=0, retry_after=3600)
        with mock.patch.object(later, "status", return_value=None), \
                mock.patch.object(later, "_launch", side_effect=launch):
            refused = later.ensure()
        self.assertEqual(len(launches), 3, "a runner started inside the pause starts no backend")
        self.assertFalse(refused["ok"])
        for reason in ("start 1 failed", "start 2 failed", "start 3 failed"):
            self.assertIn(reason, refused["detail"])
        state = self.ledger.Ledger(self.path, durable=False)
        self.books.append(state)
        self.assertEqual(state.backend_start_state()["retry_at"], refused["retry_at"])
        self.clock["now"] += 3600
        after = self.runner(start_timeout=0, retry_after=3600)
        with mock.patch.object(after, "status", return_value=None), \
                mock.patch.object(after, "_launch", side_effect=launch):
            after.ensure()
        self.assertEqual(len(launches), 4, "the pause ends")
        self.assertEqual(after.start_failures, 1)

    def test_a_slow_start_is_waited_for_by_the_next_runner_not_started_again(self) -> None:
        record = {"method": "wmi", "pid": 4242, "process_created_at": 1.0}
        first = self.runner(start_timeout=0)
        with mock.patch.object(first, "status", return_value=None), \
                mock.patch.object(first, "_launch", return_value=dict(record)):
            self.assertIn("waited for again", first.ensure()["detail"])
        second = self.runner(start_timeout=30)
        answers = iter([None, None, {"app_version": "x"}])
        with mock.patch.object(second, "status", side_effect=lambda: next(answers)), \
                mock.patch.object(second, "_launch") as launch, \
                mock.patch.object(ports, "_process_alive", return_value=True), \
                mock.patch.object(ports, "listening_pid", return_value=4242), \
                mock.patch.object(second, "config", return_value={"root": "x"}):
            result = second.ensure()
        launch.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pid"], 4242)
        self.assertEqual((second.state.load()["pending"], second.state.load()["failures"]), (None, []))

    def test_a_pending_start_whose_process_is_gone_is_started_again(self) -> None:
        first = self.runner(start_timeout=0)
        with mock.patch.object(first, "status", return_value=None), \
                mock.patch.object(first, "_launch", return_value={"method": "wmi", "pid": 4242, "process_created_at": 1.0}):
            first.ensure()
        second = self.runner(start_timeout=0)
        with mock.patch.object(second, "status", return_value=None), \
                mock.patch.object(ports, "_process_alive", return_value=False), \
                mock.patch.object(second, "_launch", return_value={"method": "wmi", "pid": 5151}) as launch:
            second.ensure()
        launch.assert_called_once()

    def test_a_broker_that_did_not_report_is_waited_for_its_start_window_and_not_started_over(self) -> None:
        """Review finding 3, case (c): the broker reported nothing within 30 s. It may have started a backend, so
        the next check (here, the next runner) waits for it until start_timeout from the launch, then may start."""
        error = ports.BackendLaunchError("the broker reported nothing within 30 s", may_have_started=True,
                                         record={"method": "wmi", "pid": None, "broker_pid": 777})
        first = self.runner(start_timeout=600)
        with mock.patch.object(first, "status", return_value=None), \
                mock.patch.object(first, "_launch", side_effect=error):
            first.ensure()
        pending = first.state.load()["pending"]
        self.assertEqual((pending["broker_pid"], pending["pid"]), (777, None))
        self.clock["now"] += 60
        second = self.runner(start_timeout=600)
        with mock.patch.object(second, "status", return_value=None), \
                mock.patch.object(second, "_launch") as launch:
            waited = second.ensure()
        launch.assert_not_called()
        self.assertIn("within 600 s of the broker's launch", waited["detail"])
        third = self.runner(start_timeout=600)
        with mock.patch.object(third, "status", return_value=None), \
                mock.patch.object(third, "_launch", side_effect=ports.BackendLaunchError("no")) as launch:
            third.ensure()
        launch.assert_called_once()

    def test_a_broker_backend_that_answers_late_is_adopted_by_the_process_tree(self) -> None:
        error = ports.BackendLaunchError("no report", may_have_started=True,
                                         record={"method": "wmi", "pid": None, "broker_pid": 777})
        first = self.runner(start_timeout=600)
        with mock.patch.object(first, "status", return_value=None), \
                mock.patch.object(first, "_launch", side_effect=error):
            first.ensure()
        second = self.runner(start_timeout=600)
        answers = iter([None, {"app_version": "x"}])
        with mock.patch.object(second, "status", side_effect=lambda: next(answers)), \
                mock.patch.object(second, "_launch") as launch, \
                mock.patch.object(ports, "listening_pid", return_value=9090), \
                mock.patch.object(ports, "process_descends_from", side_effect=lambda pid, ancestor, *a, **k: ancestor == 777), \
                mock.patch.object(ports, "_process_created_at", return_value=5.0), \
                mock.patch.object(ports, "process_in_job", return_value=False), \
                mock.patch.object(second, "config", return_value={"root": "x"}):
            result = second.ensure()
        launch.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pid"], 9090)
        self.assertEqual(json.loads(second.launch_record_path.read_text())["pid"], 9090)

    def test_origin_recognises_the_backend_of_a_broker_that_never_reported(self) -> None:
        supervisor = self.runner()
        supervisor.launch_record_path.parent.mkdir(parents=True, exist_ok=True)
        supervisor.launch_record_path.write_text(json.dumps({"method": "wmi", "pid": None, "broker_pid": 777}))
        with mock.patch.object(ports, "process_descends_from", side_effect=lambda pid, ancestor, *a, **k: ancestor == 777), \
                mock.patch.object(ports, "_process_created_at", return_value=5.0), \
                mock.patch.object(ports, "process_in_job", return_value=None):
            origin = supervisor.origin(pid=9090)
            self.assertEqual((origin["how"], origin["by"], origin["broker_pid"]), ("wmi", "this campaign's runner", 777))
            self.assertNotIn("launcher_pid", origin)
        with mock.patch.object(ports, "process_descends_from", return_value=False), \
                mock.patch.object(ports, "_process_created_at", return_value=5.0), \
                mock.patch.object(ports, "process_in_job", return_value=None):
            self.assertEqual(supervisor.origin(pid=9090)["how"], "unknown")

    def test_a_listener_outside_the_runners_process_tree_is_refused_and_named(self) -> None:
        supervisor = self.runner(start_timeout=30)
        answers = iter([None, {"app_version": "x"}])
        with mock.patch.object(supervisor, "status", side_effect=lambda: next(answers)), \
                mock.patch.object(supervisor, "_launch", return_value={"method": "wmi", "pid": 4242, "broker_pid": 777}), \
                mock.patch.object(ports, "listening_pid", return_value=9090), \
                mock.patch.object(ports, "process_descends_from", return_value=False):
            result = supervisor.ensure()
        self.assertFalse(result["ok"])
        self.assertIn("answered by pid 9090, which is not the backend this runner started nor a process it started",
                      result["detail"])


class FinishedCampaignTests(unittest.TestCase):
    """Review of 2026-10-07, finding 2: the hourly task kept starting runners on a finished campaign, each of which
    locked the Catalog and started a backend. run now exits before either, and says how to end the task."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.world = campaign_fakes.World(Path(self.directory.name), ["u1"])
        self.world.run(max_iterations=4000)
        spec = importlib.util.spec_from_file_location("campaign_runner_cli", SCRIPTS / "campaign-runner.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_cli(self) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = self.cli.main(["--workspace-root", str(self.world.workspace_root), "run", "--campaign", "test-campaign",
                                  "--until-idle"])
        return code, err.getvalue()

    def test_a_runner_on_a_finished_campaign_exits_before_any_lock_or_backend(self) -> None:
        with self.world.open() as book:
            self.assertEqual(book.unit("u1")["state"], "done")
            started_before = len(book.events("runner_started"))
        with mock.patch.object(self.cli, "Environment", side_effect=AssertionError("built an environment")):
            code, text = self.run_cli()
            again, _ = self.run_cli()
        self.assertEqual((code, again), (0, 0))
        self.assertIn('schtasks /Change /TN "MSDIAL-campaign-test-campaign" /Disable', text)
        self.assertIn('schtasks /Delete /TN "MSDIAL-campaign-test-campaign" /F', text)
        self.assertIn("no work left", text)
        with self.world.open() as book:
            self.assertEqual(len(book.events("runner_started")), started_before, "the campaign was not taken")
            self.assertEqual(len(book.events("no_work_left")), 1, "said once, not every hour")
            self.assertIsNone(book.runner()["pid"])

    def test_work_left_is_a_waiting_request_or_held_raw_data_the_runner_looks_at_again(self) -> None:
        from campaign import machine

        with self.world.open() as book:
            self.assertEqual(machine.remaining_work(book), [])
            book.update("u1", "2026-10-07T00:00:00+00:00", raw_disposition="held")
            self.assertEqual(machine.remaining_work(book), ["1 ended unit(s) hold raw data the runner looks at again"])
        with mock.patch.object(self.cli, "Environment", side_effect=ValueError("got past the check")):
            code, text = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("got past the check", text)
        with self.world.open() as book:
            book.update("u1", "2026-10-07T00:00:00+00:00", raw_disposition="released")
            book.add_request("retry", "u1", "once more", "operator", "2026-10-07T00:00:00+00:00")
            self.assertEqual(machine.remaining_work(book), ["1 operator request(s) wait for a runner"])


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
            # How to end the task once the campaign has ended: printed, never run.
            self.assertIn('schtasks /Change /TN "MSDIAL-campaign-c1" /Disable', text)
            self.assertIn('schtasks /Delete /TN "MSDIAL-campaign-c1" /F', text)
            self.assertTrue(target.read_bytes().startswith(b"\xff\xfe"), "UTF-16 with a byte-order mark")
            self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", target.read_text(encoding="utf-16"))


if __name__ == "__main__":
    unittest.main()
