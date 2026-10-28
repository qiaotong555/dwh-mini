"""把 metadata_lab 的五个函数包成 MCP 工具（stdio 传输）。

    # 手工自测（不经客户端）—— 会进入等待输入的状态，Ctrl+C 退出
    ..\\..\\.venv\\Scripts\\python.exe server.py

    # 真正使用：在 Cursor / Claude Desktop 的配置里挂上（见 README.md）

## 这个文件为什么给现成的

协议层是胶水：`@mcp.tool()` 加一个函数、返回值转成字符串。
**真正有内容的是 metadata_lab.py 里的检索打分** —— 那才是你写的部分。

## ⚠ stdio 传输下有一个致命细节

`print()` 会往 **stdout** 写，而 stdout 是 JSON-RPC 的通道。
**任何一句 print 都会让客户端解析失败**，报错通常长这样：

    MCP error -32700: Parse error

所以：
  · 调试信息一律 `print(..., file=sys.stderr)`
  · 或者用 `logging`（默认就写 stderr）
  · 这个文件里一个裸 print 都没有，别往里加

这是所有 stdio 类 MCP Server 的第一个坑，和当年写 CGI 时「不能往 stdout 乱输出」
是同一类问题。
"""
import os
import sys

# 让 `python server.py` 和 `python -m mcp_server.server` 都能 import 到同目录的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer      # noqa: E402  mcp>=2 的 API

import metadata_lab as L                         # noqa: E402

mcp = MCPServer(
    "dwh-mini-metadata",
    instructions=(
        "这是 dwh_mini 数仓的元数据服务。可以查：有哪些表、某张表的结构与上下游、"
        "指标的口径与公式、以及按关键词检索字段。"
        "回答「某个数怎么算」时先调 find_metric，回答「哪张表」时先调 list_tables。"
    ),
)


def _meta():
    """每次调用都重读 yaml —— 文件只有几 KB，重读成本可以忽略，
    换来的是「改完 yaml 不用重启 server」，开发时体验差别很大。"""
    return L.load_metadata()


@mcp.tool()
def list_tables() -> str:
    """列出数仓全部表：表名、分层（ODS/DIM/DWD/DWS/ADS）、行数、一句话说明。

    用户问「有哪些表」「数据都在哪」时先调这个。
    """
    return L.to_text(L.list_tables(_meta()))


@mcp.tool()
def describe_table(name: str) -> str:
    """查一张表的完整结构：字段名、类型、字段含义、上游来源、下游去向。

    参数 name 传表名（如 dws_trade_daily），大小写不敏感。
    用户问「这张表有哪些字段」「这个数从哪来」时用。
    """
    return L.to_text(L.describe_table(_meta(), name))


@mcp.tool()
def find_metric(name: str) -> str:
    """查指标口径：定义、SQL 公式、来源表、注意事项。

    参数 name 可以是英文名或中文别名（如 gmv / GMV / 成交额 / 支付转化率）。
    用户问「某个指标怎么算」「GMV 的定义」时用。
    """
    return L.to_text(L.find_metric(_meta(), name))


@mcp.tool()
def search_metadata(query: str) -> str:
    """按关键词检索元数据（表名、字段名、描述、指标别名），返回最相关的前 3 条。

    用户的问题里没有明确表名或指标名时（如「哪个字段表示退款」「SCD2 在哪」）用这个。
    """
    return L.to_text(L.search(_meta(), query, k=3))


if __name__ == "__main__":
    mcp.run()          # 默认 transport="stdio"
