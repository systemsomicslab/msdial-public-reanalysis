"""A stand-in MCP server over stdio for tests/test_run_triage_agent.py: answers initialize and tools/list (in two pages,
to exercise the cursor) and nothing else. Its tools are the Catalog's read tools the skill allows plus two that write,
which the wrapper must remove from the session with --disallowedTools. Synthetic; it reads and writes nothing else."""

from __future__ import annotations

import json
import sys

TOOLS = [
    "msdial_catalog_status",
    "msdial_catalog_search",
    "msdial_catalog_get_analysis_unit",
    "msdial_catalog_save_class_proposal",
    "msdial_catalog_reanalysis_handoff",
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        ident = message.get("id")
        method = message.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": ident, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-catalog", "version": "0.6.1-test"}}})
        elif method == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            page = TOOLS[3:] if cursor == "page2" else TOOLS[:3]
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in page]}
            if cursor != "page2":
                result["nextCursor"] = "page2"
            send({"jsonrpc": "2.0", "id": ident, "result": result})
        elif ident is not None:
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "not supported"}})


if __name__ == "__main__":
    main()
