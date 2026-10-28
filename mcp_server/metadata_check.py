"""①-B 验收 · 14 项，秒级。

    ..\\..\\.venv\\Scripts\\python.exe metadata_check.py

## 三层，验的东西完全不同

    yaml 层  5 项   元数据本身填完没有（**直接读 yaml，不经过你的代码**）
    逻辑层  6 项   五个函数的契约：返回结构、找不到表时返回 None 而不是崩
    协议层  3 项   server.py 声明的工具对不对、有没有往 stdout 乱 print

**yaml 层故意绕开你的实现**：如果混在一起，就分不清是「说明没填」还是「代码写错」。
这两个的诊断成本差很远。

## 断言层的四个问题（来自 README 的要求）

    ① 「支付转化率」→ pay_rate     ② 「退款」→ refund_cnt / pay_status
    ③ 「SCD2 / 拉链」→ dim_product  ④ 「GMV」→ gmv

这四问不是刁难 —— 元数据的价值就是「能被问到」。检索算法再准，
yaml 里没写「退款」这个词也找不到。
"""
import argparse
import ast
import asyncio
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
META = os.path.join(ROOT, "metadata")

_ok = _fail = _skip = 0
EXPECT_TABLES = ["ods_order", "ods_payment", "dim_product", "dwd_trade_order_detail",
                 "dws_trade_daily", "ads_metric_daily_gmv", "ads_metric_pay_rate",
                 "ads_metric_category_top"]
EXPECT_TOOLS = ["list_tables", "describe_table", "find_metric", "search_metadata"]


def chk(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name:<44}{detail}")
    else:
        _fail += 1
        print(f"  FAIL  {name:<44}{detail}")
    return cond


def skip(name, why):
    global _skip
    _skip += 1
    print(f"  SKIP  {name:<44}（{why}）")


def head(t):
    print()
    print("=" * 88)
    print(t)
    print("=" * 88)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="lab", choices=["lab", "ref"])
    args = ap.parse_args()
    mod_name = "_ref_lab" if args.target == "ref" else "metadata_lab"

    head(f"dwh_mini ①-B 验收 · 元数据 MCP Server · target={args.target}")

    # ---------------- yaml 层（不经过你的代码）----------------
    head("1 · yaml 层（元数据填完没有）")
    tp = os.path.join(META, "tables.yaml")
    mp = os.path.join(META, "metrics.yaml")
    if not (os.path.exists(tp) and os.path.exists(mp)):
        chk("tables.yaml 与 metrics.yaml 存在", False,
            f"缺 {[p for p in (tp, mp) if not os.path.exists(p)]}")
        print("\n  先跑 make_metadata.py 生成骨架。")
        return 1
    chk("tables.yaml 与 metrics.yaml 存在", True)

    tabs = (yaml.safe_load(open(tp, encoding="utf-8")) or {}).get("tables", [])
    mets = (yaml.safe_load(open(mp, encoding="utf-8")) or {}).get("metrics", [])

    names = [t["name"] for t in tabs]
    chk("8 张表齐全", set(names) == set(EXPECT_TABLES),
        "" if set(names) == set(EXPECT_TABLES) else f"缺 {set(EXPECT_TABLES)-set(names)} 多 {set(names)-set(EXPECT_TABLES)}")

    bad_desc = [t["name"] for t in tabs
                if not t.get("description") or str(t["description"]).startswith("TODO")
                or len(str(t["description"])) < 8]
    chk("每张表都有实质说明（≥8 字、非 TODO）", not bad_desc,
        f"{len(tabs)-len(bad_desc)}/{len(tabs)} 张已填" + (f"  未填 {bad_desc}" if bad_desc else ""))

    all_cols = [(t["name"], c) for t in tabs for c in t.get("columns", [])]
    filled = [(tn, c) for tn, c in all_cols
              if c.get("desc") and not str(c["desc"]).startswith("TODO")]
    ratio = len(filled) / len(all_cols) if all_cols else 0
    chk("字段说明填充率 ≥ 80%", ratio >= 0.8,
        f"{len(filled)}/{len(all_cols)} = {ratio:.0%}")

    bad_met = [m["name"] for m in mets
               if not all(m.get(k) for k in ("name", "definition", "formula", "source_table"))]
    chk("3 个指标都有 definition/formula/source_table",
        len(mets) == 3 and not bad_met,
        f"{len(mets)} 个指标" + (f"  缺字段 {bad_met}" if bad_met else ""))

    # ---------------- 逻辑层 ----------------
    head("2 · 逻辑层（五个函数的契约）")
    try:
        mod = __import__(mod_name)
    except Exception as e:                                # noqa: BLE001
        chk(f"导入 {mod_name}", False, f"{type(e).__name__}: {e}")
        return 1

    try:
        meta = mod.load_metadata()
        chk("load_metadata 返回 tables 8 / metrics 3",
            len(meta.get("tables", [])) == 8 and len(meta.get("metrics", [])) == 3,
            f"tables={len(meta.get('tables', []))} metrics={len(meta.get('metrics', []))}")
    except Exception as e:                                # noqa: BLE001
        chk("load_metadata 返回 tables 8 / metrics 3", False, f"{type(e).__name__}: {e}")
        print("\n  load_metadata 没跑通，后面依赖它的检查全部跳过。")
        return 1

    def t_list():
        rows = mod.list_tables(meta)
        keys_ok = all(set(r.keys()) == {"name", "layer", "rows", "description"} for r in rows)
        order = ["ODS", "DIM", "DWD", "DWS", "ADS"]
        layers = [r["layer"] for r in rows]
        sorted_ok = layers == sorted(layers, key=lambda x: order.index(x) if x in order else 99)
        return (len(rows) == 8 and keys_ok and sorted_ok,
                f"{len(rows)} 条，键{'对' if keys_ok else '多/少'}，顺序{'对' if sorted_ok else layers}")
    try:
        chk("list_tables：8 条 / 只含 4 键 / 按层排序", *t_list())
    except Exception as e:                                # noqa: BLE001
        chk("list_tables：8 条 / 只含 4 键 / 按层排序", False, f"{type(e).__name__}: {e}")

    def t_desc():
        d = mod.describe_table(meta, "dwd_trade_order_detail")
        ok = (d and d["name"] == "dwd_trade_order_detail" and len(d["columns"]) == 15
              and all("desc" in c for c in d["columns"]))
        return ok, f"{len(d['columns']) if d else 0} 列" if d else "返回 None"
    try:
        chk("describe_table：dwd 返回 15 列且带 desc", *t_desc())
    except Exception as e:                                # noqa: BLE001
        chk("describe_table：dwd 返回 15 列且带 desc", False, f"{type(e).__name__}: {e}")

    def t_desc_ci():
        d1 = mod.describe_table(meta, "DWS_TRADE_DAILY")
        d2 = mod.describe_table(meta, "不存在的表")
        return (d1 is not None and d2 is None), \
               f"大写{'命中' if d1 else '未命中'}，不存在表返回 {d2!r}"
    try:
        chk("describe_table：大小写不敏感 / 不存在返回 None", *t_desc_ci())
    except Exception as e:                                # noqa: BLE001
        chk("describe_table：大小写不敏感 / 不存在返回 None", False, f"{type(e).__name__}: {e}")

    def t_metric():
        a = mod.find_metric(meta, "gmv")
        b = mod.find_metric(meta, "支付转化率")
        ok = (a and a["name"] == "gmv" and b and b["name"] == "pay_rate"
              and "SUCCESS" in b["formula"])
        return ok, f"gmv→{a['name'] if a else None}，支付转化率→{b['name'] if b else None}"
    try:
        chk("find_metric：英文名 + 中文别名都能命中", *t_metric())
    except Exception as e:                                # noqa: BLE001
        chk("find_metric：英文名 + 中文别名都能命中", False, f"{type(e).__name__}: {e}")

    def t_search():
        cases = [("支付转化率", "pay_rate"), ("退款", None), ("SCD2", "dim_product"),
                 ("拉链", "dim_product"), ("GMV", "gmv")]
        got = []
        for q, expect in cases:
            r = mod.search(meta, q, k=3)
            top = r[0]["name"] if r else None
            got.append(f"{q}→{top}")
            if expect and (not top or expect not in top):
                return False, "；".join(got) + f"  期望含 {expect}"
        return True, "；".join(got)
    try:
        chk("search：5 个 query 的 top-1 都能命中", *t_search())
    except Exception as e:                                # noqa: BLE001
        chk("search：5 个 query 的 top-1 都能命中", False, f"{type(e).__name__}: {e}")

    # ---------------- 协议层 ----------------
    head("3 · 协议层（server.py 声明对不对）")

    sp = os.path.join(HERE, "server.py")
    src = open(sp, encoding="utf-8").read()

    def t_import():
        srv = __import__("server")
        tools = asyncio.run(srv.mcp.list_tools())
        names = sorted(t.name for t in tools)
        return (names == sorted(EXPECT_TOOLS), f"{len(names)} 个：{names}")
    try:
        chk("server.py 能导入并声明 4 个工具", *t_import())
    except Exception as e:                                # noqa: BLE001
        chk("server.py 能导入并声明 4 个工具", False, f"{type(e).__name__}: {str(e)[:60]}")

    def t_no_print():
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                kws = {k.arg for k in node.keywords}
                if "file" not in kws:
                    bad.append(node.lineno)
        return (not bad), ("没有裸 print" if not bad
                           else f"第 {bad} 行有裸 print —— stdio 传输下会破坏 JSON-RPC")
    try:
        chk("server.py 里没有裸 print（只能 print 到 stderr）", *t_no_print())
    except SyntaxError as e:
        chk("server.py 里没有裸 print（只能 print 到 stderr）", False, f"语法错误 {e}")

    rd = os.path.join(HERE, "README.md")
    ok_readme = (os.path.exists(rd)
                 and "cursor" in open(rd, encoding="utf-8").read().lower())
    chk("README.md 有挂载配置（含 cursor / claude desktop 示例）", ok_readme)

    print()
    print("=" * 88)
    total = _ok + _fail + _skip
    print(f"{total} 项：{_ok} 通过 / {_fail} 失败 / {_skip} 跳过")
    print("=" * 88)
    if _fail:
        print("卡住看哪：")
        print("  · yaml 层失败 → 有 TODO 没填；字段说明里**要出现**「退款」「SCD2」这类词，否则检索找不到")
        print("  · list_tables 键多了 → 你透出了整个 dict，只返回 name/layer/rows/description")
        print("  · describe_table 抛异常 → 表不存在要 return None，不能 raise")
        print("  · find_metric 不中 → 别忘了匹配 cn_name 和 aliases，并且要 lower()")
        print("  · search 不中 → 先确认 yaml 里有那个词；再查 2-gram 切词有没有做")
        print("  · 协议层失败 → 4 个工具名对不对；有没有往 stdout print")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
