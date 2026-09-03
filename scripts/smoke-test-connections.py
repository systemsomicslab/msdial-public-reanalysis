from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PYTHON = sys.executable
INTERACTIVE_SCRIPT = Path(
    os.environ.get(
        "MSDIAL_INTERACTIVE_MCP_SCRIPT",
        r"D:\0_SourceCode\msdial_interactive_app\scripts\msdial-interactive-mcp.py",
    )
)
CATALOG_DATABASE = Path(
    os.environ.get(
        "MSDIAL_REPOSITORY_CATALOG",
        r"D:\0_SourceCode\msdial_repository_catalog\catalog-data\native-smoke.sqlite",
    )
)


def payload(result: object) -> dict[str, object]:
    content = getattr(result, "content", [])
    if not content:
        return {}
    return json.loads(content[0].text)


async def inspect_server(
    name: str,
    parameters: StdioServerParameters,
    status_tool: str,
    status_arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            result = await session.call_tool(status_tool, status_arguments or {})
            return {
                "name": name,
                "tool_count": len(listing.tools),
                "tools": sorted(tool.name for tool in listing.tools),
                "status": payload(result),
            }


async def main_async() -> dict[str, object]:
    catalog_env = dict(os.environ)
    catalog_env["MSDIAL_REPOSITORY_CATALOG"] = str(CATALOG_DATABASE)
    interactive = StdioServerParameters(
        command=PYTHON,
        args=[str(INTERACTIVE_SCRIPT)],
    )
    catalog = StdioServerParameters(
        command=PYTHON,
        args=["-m", "msdial_repository_catalog.mcp_server"],
        env=catalog_env,
    )
    interactive_result, catalog_result = await asyncio.gather(
        inspect_server(
            "msdial-interactive",
            interactive,
            "msdial_interactive_status",
        ),
        inspect_server(
            "msdial-repository-catalog",
            catalog,
            "msdial_catalog_status",
        ),
    )
    return {
        "schema": "msdial-claude-mcp-smoke.v1",
        "python": PYTHON,
        "servers": [interactive_result, catalog_result],
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main_async()), ensure_ascii=False, indent=2))
