"""A stand-in for the Claude Code CLI in headless stream-JSON mode, for tests/test_run_triage_agent.py.

It takes the flags scripts/run-triage-agent.py passes, logs them (FAKE_CLAUDE_LOG), reads the prompt from stdin, emits a
canned transcript (an init message, tool_use and tool_result blocks, a result message) on stdout, and writes the spec the
prompt names (`../out/<accession>.spec.json`, or a revision's `.spec.r<N>.json`) from FAKE_CLAUDE_SPECS ({accession:
spec document}), as a skill 1.2.0 agent does: code expands it into the payloads. It writes the debug log a real CLI
writes (hooks registered, memory off) to --debug-file. FAKE_CLAUDE_SCENARIO picks a misbehaviour:

  ok        the session as it should be
  check     also calls triage_spec_check, through the spec check server the session's mcp.json names (a real call)
  payload   writes payloads (FAKE_CLAUDE_PAYLOADS: {unit_id: document}) instead of the spec, as skill 1.1.0 did
  badspec   writes a spec whose last group is missing, so rows or members are left uncovered
  record    also writes <unit>.record.json into the output folder
  fail      writes the spec, then ends in an error result with exit status 1
  hang      sleeps past any test timeout before writing anything
  outside   also writes a file into its working directory
  unclean   reports a plugin from outside the harness in its init message
  bash      also calls Bash (refused), a tool the skill does not allow
  nodeny    reports a server tool the skill does not list, as a session without --disallowedTools would
  nodebug   writes no debug log
  readout   also reads a file outside the session's folders
  pathpay   writes a spec holding an absolute path
  slowstdin sleeps 25 s before it reads its prompt

Synthetic: public accession ids, no person, no real model.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SERVER_TOOLS = {"msdial-triage-spec": ["triage_spec_check"]}


def value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def call_server(config: dict, name: str, arguments: dict) -> dict:
    """One tools/call to a stdio MCP server, as the CLI would make it."""
    process = subprocess.Popen([config["command"], *config.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    assert process.stdin and process.stdout
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}]
    for message in messages:
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    process.stdin.flush()
    answer: dict = {}
    for line in process.stdout:
        message = json.loads(line.decode("utf-8"))
        if message.get("id") == 2:
            answer = message.get("result") or {"content": [{"type": "text", "text": json.dumps(message.get("error"))}],
                                               "isError": True}
            break
    process.stdin.close()
    process.wait(timeout=60)
    return answer


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("0.0.0-fake (Claude Code)")
        return 0
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "ok")
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if scenario == "slowstdin":
        time.sleep(25)
    # A checkout with core.autocrlf renders the template with CRLF; a real agent reads either.
    prompt = sys.stdin.buffer.read().decode("utf-8").replace("\r\n", "\n")
    debug = value(args, "--debug-file")
    if debug and scenario != "nodebug":
        Path(debug).write_text("[DEBUG] Registered 0 hooks from 1 plugins\n"
                               "[DEBUG] $.fs.ancestors (cc-plugin-agents-md): project memory is off\n", encoding="utf-8")
    cwd = Path.cwd()
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": args, "cwd": str(cwd), "prompt_bytes": len(prompt.encode("utf-8")),
                                     "env": {k: os.environ.get(k) for k in (
                                         "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                                         "ENABLE_CLAUDEAI_MCP_SERVERS", "CLAUDECODE")}}) + "\n")
    session_id = value(args, "--session-id") or "no-session-id"
    model = value(args, "--model") or "fake-model-1"
    builtin = [t for t in (value(args, "--tools") or "").split(",") if t]
    denied = set((value(args, "--disallowedTools") or "").split(","))
    servers = json.loads(Path(value(args, "--mcp-config")).read_text(encoding="utf-8"))["mcpServers"]
    tools = builtin + [f"mcp__{s}__{t}" for s in servers for t in SERVER_TOOLS.get(s, [])
                       if f"mcp__{s}__{t}" not in denied]
    if scenario == "nodeny":
        tools += [f"mcp__{s}__write_anything" for s in servers]
    plugins = [{"name": "cc-plugin-sec-default", "path": "builtin", "source": "cc-plugin-sec-default@builtin"}]
    if scenario == "unclean":
        plugins.append({"name": "someone-elses-plugin", "path": "plugins/x", "source": "x@marketplace"})
    emit({"type": "system", "subtype": "init", "cwd": str(cwd), "session_id": session_id, "tools": tools,
          "mcp_servers": [{"name": s, "status": "connected"} for s in servers], "model": model,
          "permissionMode": "dontAsk", "slash_commands": [], "skills": [], "plugins": plugins,
          "claude_code_version": "0.0.0-fake"})
    if scenario == "hang":
        time.sleep(120)
    named = re.findall(r"`\.\./out/([A-Za-z0-9._-]+\.spec(?:\.r[0-9]+)?\.json)`", prompt)
    spec_name = named[0] if named else None
    accession = re.match(r"^(.+?)\.spec", spec_name).group(1) if spec_name else None
    units = [u.strip() for u in prompt.split(" and only these: ", 1)[1].split("\n", 1)[0].strip().rstrip(".").split(",")]
    out = (cwd / ".." / "out").resolve()
    inputs = (cwd / ".." / "inputs").resolve()
    calls: list[tuple[str, dict, object, bool]] = []
    skill = cwd / ".claude" / "skills" / "msdial-unit-triage" / "SKILL.md"
    calls.append(("Read", {"file_path": str(skill)},
                  "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(skill.read_text(encoding="utf-8").splitlines()[:5], 1)),
                  False))
    # Secrets, an operator's name and machine paths in a call's arguments and result: the wrapper scrubs them.
    calls.append(("Grep", {"pattern": "Mode", "path": str(inputs), "api_key": "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV",
                           "database": "C:\\Users\\Quokka Operator\\catalog\\native.sqlite"},
                  [{"type": "text", "text": json.dumps({
                      "unit_id": units[0], "database": "D:\\catalog-data\\native.sqlite",
                      "campaign_lock": {"approval_id": "pool-20261106-quokka"}, "contact": "someone@example.org"})}],
                  False))
    digest = inputs / "digest.md"
    if digest.is_file():
        calls.append(("Read", {"file_path": str(digest)}, digest.read_text(encoding="utf-8")[:2000], False))
    if scenario == "bash":
        calls.append(("Bash", {"command": "dir"}, "No such tool available: Bash", True))
    if scenario == "readout":
        calls.append(("Read", {"file_path": str((cwd / ".." / ".." / "MEMORY.md").resolve())}, "remembered", False))
    for unit in units:
        handoff = inputs / unit / "handoff.json"
        calls.append(("Read", {"file_path": str(handoff)}, handoff.read_text(encoding="utf-8")[:4000], False))
    if scenario == "payload":
        payloads = json.loads(Path(os.environ["FAKE_CLAUDE_PAYLOADS"]).read_text(encoding="utf-8"))
        for unit in units:
            calls.append(("Write", {"file_path": str(out / f"{unit}.json"),
                                    "content": json.dumps(payloads[unit], indent=1, ensure_ascii=False)}, "File created", False))
    elif spec_name:
        specs = json.loads(Path(os.environ["FAKE_CLAUDE_SPECS"]).read_text(encoding="utf-8"))
        spec = dict(specs[accession])
        if scenario == "badspec":
            spec["groups"] = spec["groups"][:-1]
        if scenario == "pathpay":
            spec["structure"] = "Read from " + str(inputs / units[0] / "samples.json")
        calls.append(("Write", {"file_path": str(out / spec_name), "content": json.dumps(spec, indent=1, ensure_ascii=False)},
                      "File created", False))
    if scenario == "record":
        calls.append(("Write", {"file_path": str(out / f"{units[0]}.record.json"), "content": "{}"}, "File created", False))
    if scenario == "outside":
        calls.append(("Write", {"file_path": str(cwd / "notes.md"), "content": "x"}, "File created", False))
    check_after = scenario == "check" and spec_name and "msdial-triage-spec" in servers
    for seq, (name, arguments, result, error) in enumerate(calls, start=1):
        ident = f"toolu_fake_{seq:03d}"
        emit({"type": "assistant", "message": {"model": model, "role": "assistant", "content": [
            {"type": "tool_use", "id": ident, "name": name, "input": arguments}]}, "session_id": session_id})
        if name == "Write":
            Path(arguments["file_path"]).write_text(arguments["content"], encoding="utf-8")
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ident, "content": result, "is_error": error}]},
            "session_id": session_id})
    if check_after:
        arguments = {"spec_file": f"../out/{spec_name}"}
        answer = call_server(servers["msdial-triage-spec"], "triage_spec_check", arguments)
        ident = f"toolu_fake_{len(calls) + 1:03d}"
        emit({"type": "assistant", "message": {"model": model, "role": "assistant", "content": [
            {"type": "tool_use", "id": ident, "name": "mcp__msdial-triage-spec__triage_spec_check", "input": arguments}]},
            "session_id": session_id})
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ident, "content": answer.get("content"),
             "is_error": bool(answer.get("isError"))}]}, "session_id": session_id})
    emit({"type": "assistant", "message": {"model": model, "role": "assistant", "content": [
        {"type": "text", "text": "done"}]}, "session_id": session_id})
    if scenario == "fail":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": session_id})
        return 1
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
          "result": "done", "permission_denials": []})
    return 0


if __name__ == "__main__":
    sys.exit(main())
