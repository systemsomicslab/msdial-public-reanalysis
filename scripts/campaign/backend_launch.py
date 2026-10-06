"""Start the campaign backend on behalf of the runner, then exit: the broker of BackendSupervisor's WMI start.

    pythonw.exe backend_launch.py <spec.json>

Windows Management Instrumentation creates this process (Win32_Process.Create), so its parent is the WMI
provider host and it sits outside the job object of whatever started the runner. It starts the backend with
the runner's own creation flags (ports.backend_creation_flags: a process group and a windowless console of
its own, which the Console, git, 7-Zip and the extractor inherit), writes the backend's process id and
creation time, and exits. The backend is then nobody's child: a tree kill of the runner does not reach it,
and neither does the end of the Claude app's job, or of a scheduled task's.

It runs under pythonw.exe, which has no console, so WMI opens no window for it.

The spec is a JSON object written by the runner: command, cwd, environment, creationflags, log (a file the
backend's output is appended to, or null for none) and result (where this writes what happened). The spec
holds the runner's environment, so it is deleted as soon as it is read. Only the standard library is used:
the broker imports nothing from Interactive, the Catalog or this repository.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_FILETIME_UNIX_EPOCH_SECONDS = 11_644_473_600


def _windows_facts(handle: int) -> dict:
    """The child's creation time (Unix seconds, as Interactive's process_liveness reads it) and whether it
    sits in a job object, read from the handle Popen holds, so no reused process id can intervene."""
    facts: dict = {}
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        times = [wintypes.FILETIME() for _ in range(4)]
        if kernel32.GetProcessTimes(wintypes.HANDLE(handle), *(ctypes.byref(value) for value in times)):
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            facts["process_created_at"] = ticks / 10_000_000 - _FILETIME_UNIX_EPOCH_SECONDS
        in_job = wintypes.BOOL()
        if kernel32.IsProcessInJob(wintypes.HANDLE(handle), None, ctypes.byref(in_job)):
            facts["in_job"] = bool(in_job.value)
    except (ImportError, OSError, AttributeError):
        pass
    return facts


def _write_result(path: str, result: dict) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    os.replace(temporary, path)


def main(argv: list[str]) -> int:
    spec_path = argv[1]
    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)
    try:
        os.remove(spec_path)
    except OSError:
        pass
    result: dict = {"broker_pid": os.getpid()}
    try:
        log = open(spec["log"], "ab") if spec.get("log") else open(os.devnull, "ab")
        try:
            process = subprocess.Popen(
                spec["command"], cwd=spec["cwd"], env=spec["environment"], stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, creationflags=int(spec.get("creationflags") or 0), close_fds=True,
            )
        finally:
            log.close()
        result["pid"] = process.pid
        if os.name == "nt":
            result.update(_windows_facts(int(process._handle)))  # noqa: SLF001 - the handle Popen holds
    except Exception as error:  # noqa: BLE001 - every failure is the runner's to report
        result["error"] = f"{type(error).__name__}: {error}"
    _write_result(spec["result"], result)
    return 0 if "pid" in result else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
