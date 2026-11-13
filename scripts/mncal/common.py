"""Run records, hashing, environment capture and machine etiquette (charter C7, C8, C10)."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

# Every run.json names these; acceptance criterion 1 compares them with the D5 pins.
ENV_PACKAGES = ("rdkit", "matchms", "myopic-mces", "numpy", "scipy", "pandas", "pyarrow", "pulp", "numba")
D5_PINS = {"rdkit": "2026.9.2", "matchms": "0.34.0", "myopic-mces": "2.3.0", "numpy": "2.5.4",
           "scipy": "1.16.3", "pandas": "3.1.1", "pyarrow": "26.0.1"}
MAX_WORKERS = 16


def sha256_file(path: str | Path, chunk: int = 1 << 22) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path: str | Path, chunk: int = 1 << 22) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_hash01(*parts: object, seed: int = 0) -> float:
    """A uniform number in [0, 1) that depends only on its inputs, for seed-fixed Poisson sampling."""
    key = "\x1f".join(str(p) for p in (seed, *parts)).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big") / 2.0**64


def package_versions() -> dict[str, str | None]:
    from importlib import metadata

    versions: dict[str, str | None] = {}
    for name in ENV_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def pin_mismatches(versions: dict[str, str | None] | None = None) -> dict[str, tuple[str, str | None]]:
    versions = versions if versions is not None else package_versions()
    return {k: (v, versions.get(k)) for k, v in D5_PINS.items() if versions.get(k) != v}


def git_commit(path: str | Path) -> dict[str, object]:
    def run(*args: str) -> str:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                              check=False).stdout.strip()

    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def catalog_pin() -> dict[str, object]:
    """The msdial_spectrum_catalog checkout this process imports from, with its commit (C10)."""
    import msdial_spectrum_catalog

    src = Path(msdial_spectrum_catalog.__file__).resolve().parents[1]
    root = src.parent
    info = git_commit(root)
    return {"name": root.name, "commit": info["commit"], "dirty": info["dirty"]}


def code_commit() -> dict[str, object]:
    return git_commit(Path(__file__).resolve().parents[2])


def environment_record() -> dict[str, object]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": package_versions(),
        "d5_pin_mismatches": pin_mismatches(),
    }


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: str | Path, payload: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: str | Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def limit_threads() -> None:
    """One BLAS/numba thread per worker; the caller decides the worker count (<= 16, C8)."""
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ.setdefault(key, "1")


def below_normal_priority() -> None:
    if os.name != "nt":
        try:
            os.nice(5)
        except OSError:
            pass
        return
    import ctypes

    BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)


def free_ram_gb() -> float | None:
    if os.name != "nt":
        return None
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullAvailPhys / 2**30
