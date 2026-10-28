"""真实 stdio 冒烟测试：启动一个 MCP 子进程，走完整握手，逐个调用四个工具。

    ..\\..\\.venv\\Scripts\\python.exe smoke_test.py

## 它和 metadata_check.py 的分工

    metadata_check.py   直接 import 函数，验**逻辑**对不对（秒级、无进程）
    smoke_test.py       走 JSON-RPC over stdio，验**协议**通不通（有进程、慢一秒）

两件事不能互相替代：
  · 函数返回对，但 `@mcp.tool()` 写错了 → 逻辑测试全绿，客户端却看不到工具
  · 工具能列出来，但返回值里有裸 print → 列工具正常，调用时才解析失败

**「我觉得应该通」不是证据，跑一遍才是。**
"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

# 四个工具 + 一个能过的最小参数
CASES = [
    ("list_tables", {}),
    ("describe_table", {"name": "dws_trade_daily"}),
    ("find_metric", {"name": "支付转化率"}),
    ("search_metadata", {"query": "退款"}),
]


def ok(name, txt, must_have=()):
    short = txt.strip().splitlines()[0][:60] if txt.strip() else "（空返回）"
    miss = [m for m in must_have if m not in txt]
    if not txt.strip():
        print(f"  FAIL  {name:<18}返回空")
        return False
    if miss:
        print(f"  FAIL  {name:<18}{short}   缺少关键词 {miss}")
        return False
    print(f"  PASS  {name:<18}{short}")
    return True


async def run():
    params = StdioServerParameters(command=sys.executable, args=[SERVER])
    passed = failed = 0

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            names = sorted(t.name for t in tools)
            expect = sorted(n for n, _ in CASES)
            if names == expect:
                print(f"  PASS  {'tools/list':<18}{len(names)} 个：{names}")
                passed += 1
            else:
                print(f"  FAIL  {'tools/list':<18}期望 {expect}，实际 {names}")
                failed += 1

            for tool, args in CASES:
                try:
                    res = await s.call_tool(tool, args)
                    txt = res.content[0].text if res.content else ""
                except Exception as e:                    # noqa: BLE001
                    print(f"  FAIL  {tool:<18}{type(e).__name__}: {str(e)[:70]}")
                    failed += 1
                    continue
                must = {"describe_table": ("dws_trade_daily", "pay_rate"),
                        "find_metric": ("pay_rate", "SUCCESS"),
                        "search_metadata": ("退款",)}.get(tool, ())
                if ok(tool, txt, must):
                    passed += 1
                else:
                    failed += 1

    print()
    print(f"  冒烟测试：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
