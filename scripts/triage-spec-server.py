"""msdial-triage-spec: a minimal stdio MCP server with one read-only tool, triage_spec_check, for the triage session.

run-triage-agent.py launches it (from the code at HEAD, written beside the session) as the only MCP server of a triage
session, so the agent can check its spec before it answers, in one cheap turn instead of a revision session. The tool
expands the spec (scripts/expand-triage-spec.py) over the session's inputs and runs validate-triage.py's payload checks
on the result (--payload-only: no record, transcript or context check), in memory, and returns at most 30 findings with
per-unit counts. It reads only the session's inputs and output folders, writes nothing there, and never calls the
network. Its calls and results are recorded and hashed with every other tool call (validate-triage.py T-TOOLS); it
checks structure and payload consistency, while what the evidence means still goes to a person.

    python -I scripts/triage-spec-server.py --inputs <session inputs> --out <session out> [--work <session work>]
        [--skill-version <x.y.z>]

A revision's scope (X-SCOPE) is not checked here: the units a revision leaves alone are not in the session; the wrapper
checks it after the session.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

SERVER_NAME = "msdial-triage-spec"
TOOL = "triage_spec_check"
SHOWN = 30
CODE_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


expander = _load("expand_triage_spec_for_server", CODE_ROOT / "scripts" / "expand-triage-spec.py")
validator = expander.vt
SERVER_VERSION = expander.EXPANDER_VERSION

TOOL_SCHEMA = {
    "name": TOOL,
    "description": ("Check a triage spec before answering: expand it over the session's inputs into the unit payloads "
                    "and run the validator's payload checks on them. Returns at most 30 findings (X-* from the expansion, "
                    "T-* from the validator) and per-unit counts. Read only."),
    "inputSchema": {
        "type": "object",
        "properties": {"spec_file": {"type": "string", "description": "The spec in the output folder, e.g. ../out/ST000001.spec.json"}},
        "required": ["spec_file"],
        "additionalProperties": False,
    },
}


def _inside(path: Path, root: Path) -> bool:
    target = os.path.normcase(os.path.realpath(str(path)))
    base = os.path.normcase(os.path.realpath(str(root)))
    return target.startswith(base.rstrip("\\/") + os.sep)


def check(spec_file: str, inputs: Path, out: Path, work: Path | None, skill_version: str) -> dict[str, Any]:
    named = Path(spec_file)
    candidates = [named] if named.is_absolute() else [*([work / named] if work else []), out / named.name]
    path = next((p for p in candidates if p.is_file() and _inside(p, out)), None)
    if path is None:
        return {"ok": False, "findings": [{"code": "X-SCHEMA", "unit": None,
                                           "message": f"{spec_file}: no such spec in the output folder (the check reads only there)"}]}
    if not validator.is_spec_file(path.name):
        return {"ok": False, "findings": [{"code": "X-SCHEMA", "unit": None,
                                           "message": f"{path.name} is not named <accession>.spec.json or .spec.r<N>.json"}]}
    ctx = expander.ExpansionContext(decided_at="1970-01-01T00:00:00+00:00", model="check", skill_version=skill_version)
    result = expander.expand(path.read_bytes(), expander.inputs_from_dir(inputs), ctx, spec_name=path.name)
    if not result.ok:
        found = [f.as_dict() for f in result.findings]
        return {"ok": False, "stage": "expansion", "findings": found[:SHOWN], "more": max(0, len(found) - SHOWN)}
    with tempfile.TemporaryDirectory(prefix="triage-spec-check-") as folder:
        directory = Path(folder)
        for unit, data in result.payloads.items():
            (directory / f"{unit}.json").write_bytes(data)
        listed = [m for tsv in sorted(inputs.glob("*.tsv")) for m in validator.read_listing(tsv)]
        catalog = validator.read_catalog(inputs) if any(inputs.glob("*/handoff.json")) else None
        report = validator.validate(directory, catalog, listed, payload_only=True)
    found = [{"code": f.check, "level": f.level, "unit": f.unit, "message": f.message} for f in report.findings
             if f.level == "error" or (f.level == "hold" and f.check not in ("T-QUEUE",))]
    counts = {unit: {**counts, "status": report.unit_status.get(unit), "verdict": report.unit_verdict(unit)}
              for unit, counts in result.counts.items()}
    return {"ok": not any(f["level"] == "error" for f in found), "stage": "validation", "findings": found[:SHOWN],
            "more": max(0, len(found) - SHOWN), "counts": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--skill-version", default="0.0.0")
    args = parser.parse_args(argv)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    def send(message: dict[str, Any]) -> None:
        stdout.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        stdout.flush()

    for raw in stdin:
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(message, dict):
            continue
        ident = message.get("id")
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if ident is None:
            continue  # a notification
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": ident, "result": {
                "protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": ident, "result": {"tools": [TOOL_SCHEMA]}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": ident, "result": {}})
        elif method == "tools/call":
            if params.get("name") != TOOL:
                send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": f"no tool {params.get('name')!r}"}})
                continue
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                answer = check(str(arguments.get("spec_file") or ""), args.inputs, args.out, args.work, args.skill_version)
                error = False
            except Exception as exc:  # noqa: BLE001 - the session gets the failure as a tool error, never a crash
                answer = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "where": traceback.format_exc(limit=2).splitlines()[-1]}
                error = True
            send({"jsonrpc": "2.0", "id": ident, "result": {
                "content": [{"type": "text", "text": json.dumps(answer, ensure_ascii=False, indent=1)}], "isError": error}})
        else:
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "not supported"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
