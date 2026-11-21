"""MCP 冒烟测试：用 stdio 客户端启动服务器，列出工具并依次调用 connect → get_overview → extract_questions。

    uv run python scripts/mcp_smoke.py            # 需要 Edge 已打开测试页
    uv run python scripts/mcp_smoke.py --list     # 只列工具（不需要浏览器）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main(list_only: bool) -> None:
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(ROOT), "zhs-exam-mcp"])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = await s.list_tools()
        print("tools:", [t.name for t in tools.tools])
        if list_only:
            return
        for name, args in [("connect", {}), ("get_overview", {}), ("extract_questions", {"nos": [1, 2]})]:
            res = await s.call_tool(name, args)
            text = "".join(getattr(c, "text", "") for c in res.content)
            print(f"\n== {name} ==\n{text[:2000]}")
            try:
                if "error" in json.loads(text):
                    break
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main("--list" in sys.argv))
