# dwh_mini · 能一键跑的数仓分层实现

把电商数仓的分层逻辑（ODS → DIM → DWD → DWS → ADS）从「需要 Hadoop + Flink 集群」
重写成 **`pip install` 之后在本机 3 秒跑完** 的版本。

不是玩具 demo：它带 **SCD2 拉链表**、**5 条数据质量断言**、以及 **26 项自动化验收**。
验收会真的往表里塞脏数据，检验断言拦不拦得住。

## 快速开始

```bash
pip install -r requirements.txt
python dwh_lab.py       # 建仓 + 打印各层行数与指标
python dwh_check.py     # 26 项验收
```

输出（节选）：

```
  ods_order                      359 行
  ods_payment                    317 行
  dim_product                     11 行     ← 10 个商品，P002 有 2 个版本
  dwd_trade_order_detail         359 行     ← 一单一行的宽表
  dws_trade_daily                  7 行
  ads_metric_daily_gmv             7 行
  ads_metric_pay_rate              7 行
  ads_metric_category_top          4 行

断言汇总：全部通过
26 项：26 通过 / 0 失败 / 0 跳过
```

## 验收 26 项（五层）

```bash
python dwh_check.py              # 全跑
python dwh_check.py --layer 3    # 只验某一层
```

| 层 | 项数 | 验什么 |
|---|---|---|
| 结构层 | 7 | 表齐不齐、行数与源表是否一致、DWD 列是否齐全 |
| SCD2 层 | 5 | 区间无重叠无空洞、每商品恰好 1 条 current、时间列是 TIMESTAMP |
| **关联层** | 4 | **9-04 前的 P002 必须关联到旧价 299；之后必须 269** |
| **对账层** | 5 | **DWS 的 GMV 要能从 DWD 重算；ADS 要能对上 DWS；类目 Top 求和 == DWD 总额** |
| **断言层** | 5 | **注入脏数据（重复主键/负金额/GMV×3/空 user_id），断言必须拦住** |

后三层是「数仓」和「ETL」的区别。能出报表只是 ETL，能让数据可信才是数仓。

### 断言层是怎么验的

在事务里注入脏数据 → 调用 `run_quality_checks()` → 断言必须返回 `passed=False` → 回滚：

```python
con.execute("BEGIN")
con.execute("UPDATE ods_order SET amount = -1.0 WHERE order_id = (SELECT MIN(order_id) ...)")
res = run_quality_checks(con)
assert res["passed"] is False       # 拦不住就 FAIL
con.execute("ROLLBACK")
```

一个永远返回 `True` 的 `check` 函数，跑起来一片绿，但它什么也没验 —— 这是这类代码最常见的假实现。

## 分层与口径

```
CSV ──► ODS ──► DIM（SCD2 拉链）──┐
        │                          ├──► DWD（宽表）──► DWS（按天）──► ADS（3 个指标）
        └──── ods_payment ─────────┘                        │
                                                             └──► 5 条质量断言
```

| 层 | 表 | 关键约定 |
|---|---|---|
| ODS | `ods_order` / `ods_payment` | **原样落地**：不去重、不过滤状态 |
| DIM | `dim_product` | 拉链：`effective_from / effective_to / is_current` |
| DWD | `dwd_trade_order_detail` | **一单一行的宽表**；维表关联必须带时间条件 |
| DWS | `dws_trade_daily` | `gmv = SUM(pay_amount WHERE pay_status='SUCCESS')` |
| ADS | 3 张指标表 | 只从 DWS/DWD 出，不回头查 ODS |

三个写死的口径：

1. **GMV 用 `pay_amount` 不用 `amount`** —— 有退款和未支付，两者差几十个百分点
2. **支付率 = 支付成功订单数 / 当天订单数**，存 0—1 小数
3. **维表关联带时间** —— `o.create_time >= d.effective_from AND o.create_time < d.effective_to`

## 两个关键设计

**① P002 在 2026-09-04 改过价（299 → 269）。**

这是整个数据集唯一的考点。如果每个商品只有一行，SCD2 就是纯装饰 ——
谁都写得出「反正不改历史」的假实现。必须有商品在中间改价，
才能验出「9-04 前的订单要关联旧价」。

对应验收：`9-04 前 10 单 unit_price = 299`，`9-04 后 15 单 = 269`。

**② 日 GMV 环比波动控制在 25% 以内。**

因为「环比突变 < 30%」是一条质量断言。如果数据本身跳 50%，断言会误报 ——
**数据要满足业务常识，断言才有意义**。
`make_data.py` 会自动试种子，挑一个波动合理的。

## 7 天指标（跑出来的真实值）

| 日期 | 订单数 | 已付 | GMV | 支付率 |
|---|---|---|---|---|
| 09-01 | 46 | 42 | 12,266.00 | 84.78% |
| 09-02 | 52 | 45 | 12,605.00 | 82.69% |
| 09-03 | 51 | 46 | 14,868.00 | 84.31% |
| 09-04 | 55 | 47 | 17,872.00 | 76.36% |
| 09-05 | 50 | 44 | 14,894.00 | 80.00% |
| 09-06 | 51 | 47 | 18,454.00 | 88.24% |
| 09-07 | 54 | 46 | 15,991.00 | 77.78% |

类目 Top：数码 52,478 / 服饰 34,092 / 食品 10,921 / 家居 9,459（合计 106,950）

## 文件

| 文件 | 说明 |
|---|---|
| `make_data.py` | 生成模拟源数据（固定种子，结果可复现） |
| `dwh_lab.py` | 分层实现：6 个函数，顺序是 ODS→DIM→DWD→DWS→ADS→断言 |
| `dwh_check.py` | 26 项验收，五层 |
| `data/*.csv` | 样本数据（37 KB，随仓库提供） |
| `metadata/*.yaml` | 数据字典：8 张表 48 个字段的说明 + 3 个指标口径 |
| `mcp_server/` | 把数据字典包成 MCP 工具，见 [mcp_server/README.md](mcp_server/README.md) |

## ①-B · 元数据 MCP Server

数仓能跑通之后，第二个问题是**别人不会用**：gmv 怎么算、哪个字段是退款、SCD2 在哪。
这部分做成了独立的 MCP 服务，让 Cursor / Claude Desktop 能直接查：

```bash
cd mcp_server
..\..\.venv\Scripts\python.exe metadata_check.py   # 14 项验收
..\..\.venv\Scripts\python.exe smoke_test.py       # 真实 stdio 冒烟测试
```

四个工具：`list_tables` / `describe_table` / `find_metric` / `search_metadata`。
挂载配置、设计取舍（为什么不引入 embedding）见 `mcp_server/README.md`。

**这一层和数仓层的分工是刻意的**：`dwh_mini` 解决「数据算得对不对」，
`mcp_server` 解决「数据能不能被用起来」。两件事分开验收、分开度量。

## 已知限制

| 限制 | 说明 |
|---|---|
| 批处理而非流处理 | 原版是 Flink 实时链路，这里按天批处理；口径一致，时效性不同 |
| 单机单文件 | DuckDB 单文件库，适合验证逻辑，不适合并发写 |
| 数据是模拟的 | 359 单 / 10 商品，为验证分层与断言设计，不代表真实分布 |
| 无调度 | 原版用 Oozie，这里是一个 python 脚本；加 Airflow 是机械改动 |
| 维度只做了商品 | 原版有 30+ 张维度表，这里取 1 张 + SCD2，证明方法而非规模 |
