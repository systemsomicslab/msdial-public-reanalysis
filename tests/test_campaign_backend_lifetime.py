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

        # Round 3 of the review: the same start as a broker that never reported records it. Under a venv the
        # broker (the venv's pythonw.exe, which WMI started) ran its script in an interpreter that has exited, so
        # the walk up from the backend breaks there; its command line and creation time still name it.
        broker, broker_created = record.get("broker_pid"), record.get("broker_created_at")
        self.assertTrue(broker and broker_created, record)
        deadline = time.monotonic() + 30
        while ports._process_alive(broker, broker_created) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertIs(ports.process_descends_from(listener, broker, broker_created), False,
                      "the process tree breaks at the broker's exited interpreter")
        supervisor.launch_record_path.write_text(json.dumps(
            {"method": "wmi", "pid": None, "broker_pid": broker, "broker_created_at": broker_created}))
        origin = self._supervisor(port, python=python).origin()
        self.assertEqual((origin["how"], origin.get("by"), origin.get("broker_pid")), ("wmi", "this campaign's runner", broker))

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


    def test_a_backend_whose_chain_broke_at_an_exited_process_is_known_by_its_command_line(self) -> None:
        """Round 3 of the 2026-10-07 review: A (the broker's launcher) waits for B (the broker's interpreter),
        which starts C (the backend's launcher) and exits; C runs D, the backend. The walk from D stops at B."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text(
                "import os, time\nopen(os.environ['PID_FILE'], 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
            pid_file = root / "pid.txt"
            supervisor = ports.BackendSupervisor(
                python=sys.executable, interactive_root=root, host="127.0.0.1", port=8798,
                jobs_file=root / "backend" / "agent-jobs.json", workspace_root=directory, start_timeout=60)
            command = supervisor.command()
            c_script = f"import subprocess, sys; sys.exit(subprocess.call({command!r}))"
            b_script = (f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {c_script!r}], "
                        f"creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))")
            a = subprocess.Popen([sys.executable, "-c", f"import subprocess, sys; subprocess.call([sys.executable, '-c', {b_script!r}])"],
                                 env={**os.environ, "PID_FILE": str(pid_file)})
            a_created = ports._process_created_at(a.pid)
            backend = None
            try:
                a.wait(timeout=60)
                deadline = time.monotonic() + 60
                while not (pid_file.is_file() and pid_file.read_text()) and time.monotonic() < deadline:
                    time.sleep(0.2)
                backend = int(pid_file.read_text())
                self.assertIs(ports.process_descends_from(backend, a.pid, a_created), False, "the walk stops at the exited B")
                self.assertIs(ports.process_runs_command(backend, command, a_created, a_created + 60), True)
                at = command.index("--port")
                other_port = [*command[:at + 1], "8797", *command[at + 2:]]
                self.assertIs(ports.process_runs_command(backend, other_port, a_created, a_created + 60), False)
                self.assertIs(ports.process_runs_command(backend, command, a_created + 3600, a_created + 3660), False,
                              "created before the recorded process: not its start")
                record = {"method": "wmi", "pid": None, "broker_pid": a.pid, "broker_created_at": a_created}
                self.assertIs(supervisor._owns(record, backend), True)
                self.assertIs(supervisor._owns({**record, "broker_created_at": a_created + 3600}, backend), False)
                # Round 4: the same backend found with no process id to start from, by what it runs and when.
                self.assertEqual([pid for pid, _ in ports.processes_running_command(command, a_created, a_created + 60)],
                                 [backend], "C runs another command; only D runs the backend's")
                self.assertEqual(ports.processes_running_command(other_port, a_created, a_created + 60), [])
                self.assertEqual(ports.processes_running_command(command, a_created + 3600, a_created + 3660), [])
                unreported = supervisor._unreported({**record, "launched_at_epoch": a_created + 30})
                self.assertIsNotNone(unreported, "a start whose broker never reported, with its backend still running")
                self.assertFalse(unreported["ok"])
                self.assertIn(f"(pid {backend}, through wmi) (the broker never reported it; found by its command line",
                              unreported["detail"])
                self.assertIn(f"taskkill /PID {backend} /T /F", unreported["detail"])
                self.assertEqual(supervisor.state.load()["pending"]["pid"], backend)
                supervisor.port = 8797
                self.assertIs(supervisor._owns(record, backend), False, "another port's backend is not this one")
                self.assertIsNone(supervisor._unreported({**record, "launched_at_epoch": a_created + 30}),
                                  "another port's command: nothing of this start runs, and the broker A has exited")
            finally:
                if backend:
                    try:
                        import psutil

                        launcher = psutil.Process(backend).ppid()
                    except Exception:  # noqa: BLE001
                        launcher = None
                    for pid in (backend, launcher):
                        if not pid:
                            continue
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
                        else:
                            with contextlib.suppress(OSError):
                                os.kill(pid, 9)
                if a.poll() is None:
                    a.kill()
                    a.wait(timeout=30)


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
                self.assertIn("left running, and no second backend is started beside it", first["detail"])
                # A runner given a longer start timeout waits for it until that long after its launch.
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
        # No real port or process table: a test that needs a listener or a process running the backend's command
        # patches these again inside its own block.
        self.stack.enter_context(mock.patch.object(ports, "listening_pid", return_value=None))
        self.stack.enter_context(mock.patch.object(ports, "processes_running_command", return_value=[]))

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
            self.assertIn("no second backend is started beside it", first.ensure()["detail"])
        second = self.runner(start_timeout=30)  # a longer start timeout: still inside its window
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


    def test_a_hung_backend_is_counted_paused_and_named_not_waited_for_by_every_runner(self) -> None:
        """Round 3 of the 2026-10-07 review: a backend that runs and never answers was waited for, 300 s at a time,
        by every later runner, adding a failure each time, and the pause, checked after it, never engaged."""
        launches, alive = [], {"value": True}

        def launch():
            launches.append(1)
            return {"method": "wmi", "pid": 4242 + len(launches), "process_created_at": 1.0}

        start = self.clock["now"]
        results, waited = [], []
        for index in range(30):
            self.clock["now"] = start + 900 * index  # a runner every 15 minutes
            supervisor = self.runner(start_timeout=300, retry_after=3600)
            begun = self.clock["now"]
            with mock.patch.object(supervisor, "status", return_value=None), \
                    mock.patch.object(supervisor, "_launch", side_effect=launch), \
                    mock.patch.object(ports, "_process_alive", side_effect=lambda *a, **k: alive["value"]):
                result = supervisor.ensure()
            waited.append(self.clock["now"] - begun)
            results.append(result)
            self.assertFalse(result["ok"])
            self.assertLessEqual(supervisor.start_failures, 3, f"runner {index}")
        self.assertEqual(len(launches), 1, "no second backend is started beside the one that still runs")
        self.assertGreaterEqual(waited[0], 300)
        self.assertEqual(waited[1:], [0.0] * 29, "only the runner that started it waits for it")
        self.assertIn("still runs and has not answered within 300 s", results[1]["detail"])
        self.assertIn("taskkill /PID 4243 /T /F", results[1]["detail"])
        self.assertIn("retry_at", results[2], "the third failure in a row starts the pause")
        self.assertIn("could not be started 3 times in a row", results[3]["detail"])
        self.assertIn("No start is tried for another", results[3]["detail"])
        self.assertIn("taskkill /PID 4243", results[3]["detail"], "the refusal names the process to end")
        paused = [index for index, result in enumerate(results) if "No start is tried for another" in result["detail"]
                  and "start_failures" in result and "retry_at" in result]
        self.assertGreaterEqual(len(paused), 10, "runners inside the pause are refused")
        self.assertLess(max(len(result["detail"]) for result in results), 3000, "the detail does not grow without end")
        # Once it has been ended, the next runner after the pause starts a new one.
        alive["value"] = False
        self.clock["now"] += 3600
        supervisor = self.runner(start_timeout=300, retry_after=3600)
        with mock.patch.object(supervisor, "status", return_value=None), \
                mock.patch.object(supervisor, "_launch", side_effect=launch), \
                mock.patch.object(ports, "_process_alive", return_value=False):
            supervisor.ensure()
        self.assertEqual(len(launches), 2)

    def test_a_start_still_waited_for_does_not_get_round_the_pause(self) -> None:
        error = ports.BackendLaunchError("no report", may_have_started=True,
                                         record={"method": "wmi", "pid": None, "broker_pid": 777})
        for index in range(3):
            if index:
                self.clock["now"] += 700  # the earlier pending start has expired by the next runner
            supervisor = self.runner(start_timeout=600, retry_after=3600)
            with mock.patch.object(supervisor, "status", return_value=None), \
                    mock.patch.object(supervisor, "_launch", side_effect=error):
                supervisor.ensure()
        self.assertIsNotNone(supervisor.state.load()["pending"], "the third start is still waited for")
        later = self.runner(start_timeout=600, retry_after=3600)
        with mock.patch.object(later, "status", return_value=None), \
                mock.patch.object(later, "_await") as waited, mock.patch.object(later, "_launch") as launched:
            refused = later.ensure()
        waited.assert_not_called()
        launched.assert_not_called()
        self.assertIn("could not be started 3 times in a row", refused["detail"])

    def test_a_broker_backend_whose_process_tree_broke_is_adopted_by_its_command_line(self) -> None:
        """Round 3 of the 2026-10-07 review: under a venv Python the walk from the backend stops at the broker's
        exited interpreter. The listener's own command line and creation time say it is this start's."""
        created = self.clock["now"]
        error = ports.BackendLaunchError("no report", may_have_started=True, record={
            "method": "wmi", "pid": None, "broker_pid": 777, "broker_created_at": created})
        for matches in (True, False):
            first = self.runner(start_timeout=600)
            with mock.patch.object(first, "status", return_value=None), \
                    mock.patch.object(first, "_launch", side_effect=error):
                first.ensure()
            second = self.runner(start_timeout=600)
            answers = iter([None, {"app_version": "x"}])
            calls = []
            with mock.patch.object(second, "status", side_effect=lambda: next(answers)), \
                    mock.patch.object(second, "_launch") as launch, \
                    mock.patch.object(ports, "listening_pid", return_value=9090), \
                    mock.patch.object(ports, "process_descends_from", return_value=False), \
                    mock.patch.object(ports, "process_runs_command",
                                      side_effect=lambda *a: (calls.append(a), matches)[1]), \
                    mock.patch.object(ports, "_process_created_at", return_value=created + 40), \
                    mock.patch.object(ports, "process_in_job", return_value=False), \
                    mock.patch.object(second, "config", return_value={"root": "x"}):
                result = second.ensure()
            launch.assert_not_called()
            self.assertEqual(calls, [(9090, second.command(), created, created + 600 + second.OWNED_START_SECONDS)])
            if matches:
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["pid"], 9090)
                record = json.loads(second.launch_record_path.read_text())
                self.assertEqual((record["pid"], record["broker_pid"]), (9090, 777))
            else:
                self.assertFalse(result["ok"])
                self.assertIn("answered by pid 9090, which is not the backend this runner started", result["detail"])

    # ---- round 4 of the 2026-10-07 review ----

    def _unreported_start(self, created: float) -> None:
        """A first runner whose WMI broker (pid 777, created at `created`) reported nothing within 30 s."""
        error = ports.BackendLaunchError("no report", may_have_started=True, record={
            "method": "wmi", "pid": None, "broker_pid": 777, "broker_created_at": created})
        first = self.runner(start_timeout=600, retry_after=3600)
        with mock.patch.object(first, "status", return_value=None), \
                mock.patch.object(first, "_launch", side_effect=error):
            first.ensure()

    def test_a_hung_backend_of_a_broker_that_never_reported_is_found_by_its_port_and_named(self) -> None:
        """Past the deadline the pending start was dropped and a second backend launched beside the hung one, which
        was never named. Its backend holds the port without answering: it is this start's (_owns), and is named."""
        created = self.clock["now"]
        self._unreported_start(created)
        self.clock["now"] += 700
        launches = []
        for index in range(3):
            supervisor = self.runner(start_timeout=600, retry_after=3600)
            with mock.patch.object(supervisor, "status", return_value=None), \
                    mock.patch.object(supervisor, "_launch", side_effect=lambda: launches.append(1)), \
                    mock.patch.object(ports, "listening_pid", return_value=9090), \
                    mock.patch.object(ports, "process_descends_from", side_effect=lambda pid, ancestor, *a, **k: ancestor == 777), \
                    mock.patch.object(ports, "_process_created_at", return_value=created + 40), \
                    mock.patch.object(ports, "_process_alive", return_value=True):
                result = supervisor.ensure()
            self.assertFalse(result["ok"])
            self.clock["now"] += 900
            if index == 0:
                self.assertIn("(pid 9090, through wmi) (the broker never reported it; found by the port it holds, "
                              "broker pid 777) still runs", result["detail"])
                self.assertIn("taskkill /PID 9090 /T /F", result["detail"])
                pending = supervisor.state.load()["pending"]
                self.assertEqual((pending["pid"], pending["process_created_at"], pending["broker_pid"]),
                                 (9090, created + 40, 777))
        self.assertEqual(launches, [], "no second backend is started beside the hung one")
        self.assertIn("No start is tried for another", result["detail"], "the third check starts the pause")
        self.assertIn("taskkill /PID 9090", result["detail"])

    def test_a_hung_backend_that_holds_no_port_is_found_by_its_command_line_and_window(self) -> None:
        created = self.clock["now"]
        self._unreported_start(created)
        self.clock["now"] += 700
        supervisor = self.runner(start_timeout=600, retry_after=3600)
        calls = []
        with mock.patch.object(supervisor, "status", return_value=None), \
                mock.patch.object(supervisor, "_launch") as launch, \
                mock.patch.object(ports, "processes_running_command",
                                  side_effect=lambda *a: (calls.append(a), [(4321, created + 20), (4322, created + 21)])[1]), \
                mock.patch.object(ports, "_process_alive", return_value=True):
            result = supervisor.ensure()
        launch.assert_not_called()
        self.assertEqual(calls, [(supervisor.command(), created, created + 600 + supervisor.OWNED_START_SECONDS)])
        self.assertIn("(pid 4321, through wmi) (the broker never reported it; found by its command line and creation "
                      "time, broker pid 777)", result["detail"], "the earliest: a launcher before its interpreter")
        self.assertIn("taskkill /PID 4321 /T /F", result["detail"])
        self.assertEqual(supervisor.state.load()["pending"]["pid"], 4321)

    def test_a_broker_that_still_runs_is_named_and_nothing_is_started_beside_it(self) -> None:
        created = self.clock["now"]
        self._unreported_start(created)
        self.clock["now"] += 700
        supervisor = self.runner(start_timeout=600, retry_after=3600)
        probed = []
        with mock.patch.object(supervisor, "status", return_value=None), \
                mock.patch.object(supervisor, "_launch") as launch, \
                mock.patch.object(ports, "_process_alive", side_effect=lambda *a: (probed.append(a), True)[1]):
            result = supervisor.ensure()
        launch.assert_not_called()
        self.assertIn((777, created), probed, "the broker is asked with its recorded creation time")
        self.assertIn("The broker WMI created (pid 777)", result["detail"])
        self.assertIn("taskkill /PID 777 /T /F", result["detail"])
        self.assertIsNone(supervisor.state.load()["pending"]["pid"], "still a start with no backend process id")
        # Once the broker has ended too, and nothing of the start is found, a new one is started.
        self.clock["now"] += 900
        later = self.runner(start_timeout=600, retry_after=3600)
        with mock.patch.object(later, "status", return_value=None), \
                mock.patch.object(later, "_launch", side_effect=ports.BackendLaunchError("no")) as launch, \
                mock.patch.object(ports, "_process_alive", return_value=False):
            later.ensure()
        launch.assert_called_once()

    def test_a_broker_with_no_recorded_creation_time_does_not_hold_back_a_start(self) -> None:
        """Without the broker's creation time its process id could be another process's by now."""
        self._unreported_start(None)  # type: ignore[arg-type]
        self.clock["now"] += 700
        supervisor = self.runner(start_timeout=600, retry_after=3600)
        with mock.patch.object(supervisor, "status", return_value=None), \
                mock.patch.object(supervisor, "_launch", side_effect=ports.BackendLaunchError("no")) as launch, \
                mock.patch.object(ports, "_process_alive", return_value=True):
            supervisor.ensure()
        launch.assert_called_once()

    def test_a_runner_waiting_for_a_broker_start_names_its_hung_backend_at_the_deadline(self) -> None:
        created = self.clock["now"]
        self._unreported_start(created)
        self.clock["now"] += 60
        supervisor = self.runner(start_timeout=600, retry_after=3600)
        with mock.patch.object(supervisor, "status", return_value=None), \
                mock.patch.object(supervisor, "_launch") as launch, \
                mock.patch.object(ports, "processes_running_command", return_value=[(4321, created + 20)]), \
                mock.patch.object(ports, "_process_alive", return_value=True):
            result = supervisor.ensure()
        launch.assert_not_called()
        self.assertIn("(pid 4321, through wmi)", result["detail"])
        self.assertIn("taskkill /PID 4321 /T /F", result["detail"])
        self.assertEqual(supervisor.start_failures, 2, "the broker's silence, then the hung backend: one failure each")
        self.assertEqual(supervisor.state.load()["pending"]["pid"], 4321)

    def test_the_pause_refusal_asks_again_whether_the_process_it_names_still_runs(self) -> None:
        """The refusal repeated 'taskkill /PID <pid>' for an hour from the stored reasons, and Windows reuses
        process ids: /T on a reused id ends an unrelated process tree."""
        record = {"method": "wmi", "pid": 4243, "process_created_at": 1_000.0}
        state = {"alive": True}
        for _ in range(4):
            supervisor = self.runner(start_timeout=300, retry_after=3600)
            with mock.patch.object(supervisor, "status", return_value=None), \
                    mock.patch.object(supervisor, "_launch", return_value=dict(record)), \
                    mock.patch.object(ports, "_process_alive", side_effect=lambda *a, **k: state["alive"]):
                result = supervisor.ensure()
            self.clock["now"] += 600
        self.assertIn("No start is tried for another", result["detail"])
        self.assertEqual(result["detail"].count("taskkill /PID 4243 /T /F"), 1, "named once, not once per failure")
        self.assertIn("created 1970-01-01T00:16:40+00:00", result["detail"])
        stored = supervisor.state.load()["failures"]
        self.assertEqual([failure.get("process") for failure in stored], [{"pid": 4243, "created_at": 1_000.0}] * 3)
        self.assertFalse(any("taskkill" in failure["reason"] for failure in stored), "the stored reasons carry no taskkill")
        for alive, said, absent in ((False, "has ended since; there is nothing to end", "taskkill"),
                                    (None, "could not be read: end it (taskkill /PID 4243 /T /F) only if a process with "
                                           "that id and creation time still runs", None)):
            state["alive"] = alive
            inside = self.runner(start_timeout=300, retry_after=3600)
            with mock.patch.object(inside, "status", return_value=None), \
                    mock.patch.object(inside, "_launch") as launch, \
                    mock.patch.object(ports, "_process_alive", side_effect=lambda *a, **k: state["alive"]):
                refused = inside.ensure()
            launch.assert_not_called()
            self.assertIn("No start is tried for another", refused["detail"])
            self.assertIn(said, refused["detail"])
            if absent:
                self.assertNotIn(absent, refused["detail"])


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

    def _require_catalog(self):
        """The Catalog's campaign_lock, imported as the runner imports it (DEFAULT_CATALOG_ROOT, then an installed
        package), or a skip: a finished campaign checks the Catalog lock, and the suite skips what needs a Catalog
        that is not here (round 4 of the 2026-10-07 review: these tests failed without one)."""
        source = Path(self.cli.DEFAULT_CATALOG_ROOT) / "src"
        if source.is_dir() and str(source) not in sys.path:
            sys.path.insert(0, str(source))
        try:
            from msdial_repository_catalog import campaign_lock
        except ImportError as error:
            self.skipTest(f"the Catalog (MSDIAL_CATALOG_ROOT) cannot be imported here: {error}")
        return campaign_lock, source

    def test_a_runner_on_a_finished_campaign_exits_before_any_lock_or_backend(self) -> None:
        self._require_catalog()
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

    # ---- the Catalog lock a dead runner left (round 3 of the 2026-10-07 review) ----

    LOCK_HOLDER = textwrap.dedent("""
        import sys, time
        sys.dont_write_bytecode = True
        sys.path.insert(0, sys.argv[1])
        from msdial_repository_catalog import campaign_lock
        campaign_lock.acquire_campaign_lock(sys.argv[2], sys.argv[3], campaign_id="test-campaign", write_wait_seconds=30)
        print("held", flush=True)
        if sys.argv[4] == "stay":
            time.sleep(120)
    """)

    def _catalog(self):
        campaign_lock, source = self._require_catalog()
        if not (source / "msdial_repository_catalog" / "campaign_lock.py").is_file():
            self.skipTest("the Catalog checkout (MSDIAL_CATALOG_ROOT) is not here: the lock holder imports it from there")
        with self.world.open() as book:
            return campaign_lock, book.campaign()["catalog_database"], book.approval()["approval_id"], source

    def _hold(self, source: Path, database: str, approval: str, *, stay: bool) -> subprocess.Popen:
        holder = subprocess.Popen([sys.executable, "-c", self.LOCK_HOLDER, str(source), database, approval,
                                   "stay" if stay else "exit"], stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        if not stay:
            holder.wait(timeout=60)
            holder.stdout.close()
        return holder

    def test_a_finished_campaign_releases_the_catalog_lock_its_dead_runner_left(self) -> None:
        """The last runner died holding the Catalog after the last unit ended. Only run released such a lock, and
        the early exit came before it: every catalog update was refused for good."""
        campaign_lock, database, approval, source = self._catalog()
        holder = self._hold(source, database, approval, stay=False)
        status = campaign_lock.campaign_lock_status(database)
        self.assertEqual((status["locked"], status["owner"], status["approval_id"]), (True, "dead", approval))
        with mock.patch.object(self.cli, "Environment", side_effect=AssertionError("built an environment")):
            code, text = self.run_cli()
            again, text_again = self.run_cli()
        self.assertEqual((code, again), (0, 0))
        self.assertIn(f"Released the Catalog's campaign lock that approval {approval} held", text)
        self.assertNotIn("Released", text_again)
        self.assertFalse(campaign_lock.campaign_lock_status(database)["locked"])
        campaign_lock.refuse_while_campaign_locked(database, "a test update")  # no longer refused
        with self.world.open() as book:
            events = book.events("catalog_lock_released")
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0]["detail_json"])["owner_pid"], holder.pid)
            self.assertEqual(len(book.events("no_work_left")), 1)
            self.assertIsNone(book.runner()["pid"], "the campaign was not taken")

    def test_a_finished_campaign_leaves_another_approvals_lock_and_a_live_owners_lock(self) -> None:
        campaign_lock, database, approval, source = self._catalog()
        self._hold(source, database, "another-approval", stay=False)
        with mock.patch.object(self.cli, "Environment", side_effect=AssertionError("built an environment")):
            code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("held by another approval, and is left as it is", text)
        status = campaign_lock.campaign_lock_status(database)
        self.assertEqual((status["locked"], status["approval_id"], status["owner"]), (True, "another-approval", "dead"))
        campaign_lock.release_campaign_lock(database, "another-approval")

        live = self._hold(source, database, approval, stay=True)
        try:
            with mock.patch.object(self.cli, "Environment", side_effect=AssertionError("built an environment")):
                code, text = self.run_cli()
            self.assertEqual(code, 0)
            self.assertIn("is left as it is", text)
            status = campaign_lock.campaign_lock_status(database)
            self.assertEqual((status["locked"], status["owner"]), (True, "alive"))
        finally:
            live.kill()
            live.wait(timeout=30)
            live.stdout.close()
        campaign_lock.release_campaign_lock(database, approval)
        with self.world.open() as book:
            self.assertEqual(book.events("catalog_lock_released"), [])

    def test_a_catalog_lock_that_cannot_be_checked_is_an_environment_failure(self) -> None:
        self._require_catalog()
        with mock.patch.object(self.cli, "Environment", side_effect=AssertionError("built an environment")), \
                mock.patch("campaign.ports.release_stale_campaign_lock", side_effect=OSError("unreadable")):
            code, text = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("The Catalog's campaign lock could not be checked (OSError: unreadable)", text)

    def test_a_catalog_that_cannot_be_imported_is_an_environment_failure(self) -> None:
        """Runs without a Catalog: the import is what fails."""
        blocked = {"msdial_repository_catalog": None, "msdial_repository_catalog.campaign_lock": None}
        with mock.patch.object(self.cli, "Environment", side_effect=AssertionError("built an environment")), \
                mock.patch.dict(sys.modules, blocked):
            code, text = self.run_cli()
        self.assertEqual(code, 3)
        self.assertRegex(text, r"The Catalog's campaign lock could not be checked \((ModuleNotFound|Import)Error")
        self.assertNotIn("another approval", text)

    def test_an_unreadable_or_corrupt_catalog_lock_is_an_environment_failure_not_another_approvals(self) -> None:
        """Round 4 of the 2026-10-07 review: campaign_lock_status reads such a lock as readable False with no approval
        id, and the runner said 'held by another approval' and exited 0, the opposite of what the PR claimed."""
        campaign_lock, _source = self._require_catalog()
        with self.world.open() as book:
            database = book.campaign()["catalog_database"]
        lock = campaign_lock.campaign_lock_path(database)
        lock.parent.mkdir(parents=True, exist_ok=True)
        for make, said in ((lambda: lock.write_text("{not json", encoding="utf-8"), "is not a lock record"),
                           (lambda: lock.mkdir(), "could not be read")):
            make()
            try:
                status = campaign_lock.campaign_lock_status(database)
                self.assertEqual((status["locked"], status["readable"]), (True, False))
                with mock.patch.object(self.cli, "Environment", side_effect=AssertionError("built an environment")):
                    code, text = self.run_cli()
                self.assertEqual(code, 3, text)
                self.assertIn("The Catalog's campaign lock could not be checked: A campaign lock exists at", text)
                self.assertIn(said, text)
                self.assertIn("left as it is, and every catalog update stays refused", text)
                self.assertNotIn("another approval", text)
                self.assertTrue(campaign_lock.campaign_lock_status(database)["locked"], "the lock is left as it is")
            finally:
                if lock.is_dir():
                    lock.rmdir()
                else:
                    lock.unlink(missing_ok=True)
        with self.world.open() as book:
            self.assertEqual(book.events("catalog_lock_released"), [])

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
