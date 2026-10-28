# dwh_mini 元数据 MCP Server

把 `dwh_mini` 的数据字典（8 张表 / 48 个字段 / 3 个指标）包成 MCP 工具，
让 Cursor / Claude Desktop 这类客户端能直接回答：

```
你：dws_trade_daily 的 gmv 是怎么算的？
模型：gmv（成交总额）口径是「当日支付成功的实际支付金额之和」，
      公式 SUM(pay_amount) WHERE pay_status = 'SUCCESS'，
      来源表 dws_trade_daily → 指标表 ads_metric_daily_gmv。
      注意：用 pay_amount 不用 amount，退款和未支付都不计入。
```

## 四个工具

| 工具 | 干什么 | 典型问题 |
|---|---|---|
| `list_tables` | 列出 8 张表（分层 / 行数 / 一句话说明） | 「这个数仓有哪些表」 |
| `describe_table` | 一张表的字段、含义、上下游 | 「这张表有哪些字段 / 这个数从哪来」 |
| `find_metric` | 指标口径、SQL 公式、注意事项 | 「GMV 怎么算 / 支付转化率是什么」 |
| `search_metadata` | 按关键词检索元数据，返回前 3 条 | 「哪个字段表示退款 / SCD2 在哪」 |

## 挂到客户端

### Cursor

配置文件：`%USERPROFILE%\.cursor\mcp.json`（全局）或项目下 `.cursor/mcp.json`

```json
{
  "mcpServers": {
    "dwh-metadata": {
      "command": "C:\\Users\\乔佟\\PycharmProjects\\Pythonfudation\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\乔佟\\PycharmProjects\\Pythonfudation\\dwh_mini\\mcp_server\\server.py"]
    }
  }
}
```

保存后重启 Cursor，在设置 → MCP 里应该看到 `dwh-metadata` 是绿色。

### Claude Desktop

配置文件：`%APPDATA%\Claude\claude_desktop_config.json`

同样的 `mcpServers` 段，路径换成你的实际路径。改完**完全退出**再启动（不是关窗口）。

## 自测

### ① 冒烟测试（不用客户端）

```bash
..\..\.venv\Scripts\python.exe smoke_test.py
```

它会启动一个真实的 stdio 子进程、走完整的 JSON-RPC 握手、调四个工具各一次。
**这是「协议层真的通」的证据**，不是「我觉得应该通」。

### ② 手工启动

```bash
..\..\.venv\Scripts\python.exe server.py
```

会进入等待输入的状态（正常），Ctrl+C 退出。

## ⚠ 一个必须知道的坑

**stdio 传输下，`print()` 会破坏协议。**

stdout 是 JSON-RPC 的通道，任何一句 print 都会让客户端解析失败：

```
MCP error -32700: Parse error
```

所以调试信息一律写 stderr：

```python
print("debug", file=sys.stderr)
```

`metadata_check.py` 有一项专门用 AST 检查 `server.py` 里有没有裸 `print` ——
这个检查很土，但它防的是最难查的一类问题（客户端只说"解析错误"，不告诉你是哪句 print）。

## 文件

| 文件 | 说明 |
|---|---|
| `metadata_lab.py` | 五个函数：加载 / 列表 / 描述 / 查指标 / 检索 |
| `server.py` | MCP 协议层：四个 `@mcp.tool()`，加一个函数就是加一个工具 |
| `make_metadata.py` | 从 `dwh.duckdb` 同步表结构到 `tables.yaml`（保留人工说明） |
| `metadata_check.py` | 14 项验收 |
| `smoke_test.py` | 真实 stdio 冒烟测试 |

## 设计取舍

**① 不引入 embedding。** 元数据只有 8 张表 48 个字段，关键词 + 中文 2-gram 足够。
代价是 `clone` 下来零模型依赖、零下载。**能不上模型就不上。**

**② 结构自动、含义人写。** `make_metadata.py` 从 `information_schema` 读字段和类型，
人只写「这个字段是什么意思」。表结构改了重跑一次，人工说明不会丢。

**③ 找不到就返回 None，不抛异常。** 这个接口的调用方是模型，模型传错表名是常事。
抛异常会让整个工具调用失败；返回 None 能回一句「没找到，你是不是想问…」。
**给机器用的接口，错误要能表达，不要只能崩。**

## 已知限制

| 限制 | 说明 |
|---|---|
| 只读 | 四个工具都是查询，不支持改元数据 |
| 无向量检索 | 关键词 + 2-gram，同义改写能力弱（说「营业额」找不到「gmv」，除非写进 aliases） |
| 元数据手写 | `tables.yaml` 的说明靠人维护，表结构变了要重跑 `make_metadata.py` |
| 单进程 | stdio 单客户端，多人共用要改 streamable-http |
