"""从 dwh.duckdb 的真实 schema 生成 metadata/tables.yaml 的**结构骨架**。

    ..\\..\\.venv\\Scripts\\python.exe make_metadata.py

它只做一件事：把 8 张表的表名、层、字段名、字段类型、行数抄进 yaml，
把「业务说明」留成 `# TODO` 让你填。

## 为什么要自动生成结构

手抄 63 个字段容易写错类型（比如 `paid_cnt` 是 HUGEINT 不是 BIGINT），
而且**表结构一旦改动，手抄的 yaml 就过期了**。

正确做法是这个脚本：**结构从数据库读，含义人来写。**
表结构变了就重跑一次，你写的 description 会保留（脚本按字段名合并，不会覆盖已有说明）。

## 依赖

先跑过 `dwh_lab.py`，确保 `dwh.duckdb` 存在。
"""
import os
import re

import duckdb
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "dwh.duckdb")
OUT_DIR = os.path.join(ROOT, "metadata")
OUT = os.path.join(OUT_DIR, "tables.yaml")

# 每张表在分层里的位置与上下游（这是结构信息，来自 dwh_lab.py 的实现，不是猜的）
LAYERS = {
    "ods_order":              ("ODS", [], ["dwd_trade_order_detail"]),
    "ods_payment":            ("ODS", [], ["dwd_trade_order_detail"]),
    "dim_product":            ("DIM", [], ["dwd_trade_order_detail"]),
    "dwd_trade_order_detail": ("DWD", ["ods_order", "ods_payment", "dim_product"],
                               ["dws_trade_daily"]),
    "dws_trade_daily":        ("DWS", ["dwd_trade_order_detail"],
                               ["ads_metric_daily_gmv", "ads_metric_pay_rate"]),
    "ads_metric_daily_gmv":   ("ADS", ["dws_trade_daily"], []),
    "ads_metric_pay_rate":    ("ADS", ["dws_trade_daily"], []),
    "ads_metric_category_top": ("ADS", ["dwd_trade_order_detail"], []),
}

ORDER = ["ods_order", "ods_payment", "dim_product", "dwd_trade_order_detail",
         "dws_trade_daily", "ads_metric_daily_gmv", "ads_metric_pay_rate",
         "ads_metric_category_top"]


def load_existing():
    """已有 yaml 里的说明要保留，不能每次重跑都把人工写的内容冲掉。"""
    if not os.path.exists(OUT):
        return {}
    with open(OUT, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    return {t["name"]: t for t in doc.get("tables", [])}


def main():
    if not os.path.exists(DB):
        raise SystemExit("找不到 dwh.duckdb，先在 dwh_mini 目录跑一次 dwh_lab.py")
    con = duckdb.connect(DB, read_only=True)
    old = load_existing()
    tables = []

    for name in ORDER:
        n_row = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position", [name]).fetchall()

        old_cols = {c["name"]: c.get("desc", "") for c in (old.get(name) or {}).get("columns", [])}
        layer, up, down = LAYERS[name]

        tables.append({
            "name": name,
            "layer": layer,
            "rows": n_row,
            # 已有的说明保留；没有就留 TODO
            "description": (old.get(name) or {}).get(
                "description") or "TODO 一句话说明这张表存什么",
            "upstream": up,
            "downstream": down,
            "columns": [
                {"name": c, "type": t,
                 "desc": old_cols.get(c) or "TODO"}
                for c, t in cols
            ],
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# dwh_mini 元数据 · 表结构 + 业务说明\n")
        f.write("# 结构由 mcp_server/make_metadata.py 从 dwh.duckdb 自动生成；\n")
        f.write("# 带 TODO 的地方是人写的部分（description / columns[].desc），重跑脚本不会覆盖。\n\n")
        yaml.safe_dump({"tables": tables}, f, allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=100)

    n_todo = sum(1 for t in tables for c in t["columns"] if c["desc"] == "TODO")
    n_todo += sum(1 for t in tables if t["description"].startswith("TODO"))
    print(f"写出 {OUT}")
    print(f"  {len(tables)} 张表，{sum(len(t['columns']) for t in tables)} 个字段，待填 {n_todo} 处")
    print()
    print("下一步：把 yaml 里的 TODO 换成真实说明，然后跑 metadata_check.py")


if __name__ == "__main__":
    main()
