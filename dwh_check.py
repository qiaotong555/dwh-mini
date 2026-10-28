"""dwh_mini 验收 · 26 项，秒级。

    ..\\.venv\\Scripts\\python.exe dwh_check.py              # 全跑（做完一层就能看到一层）
    ..\\.venv\\Scripts\\python.exe dwh_check.py --layer 1    # 只验结构层
    ..\\.venv\\Scripts\\python.exe dwh_check.py --layer 2    # 只验 SCD2 层 …

## 这个脚本验什么

不是「代码跑不跑得通」，是**五层里每一层有没有真的做对**：

    结构层  7 项   表齐不齐、行数对不对（无损落地）
    SCD2 层 5 项   区间有无重叠/空洞、每商品 1 条 current、时间列类型
    关联层  4 项   ★ 9-04 前的订单有没有关联到旧价格
    对账层  5 项   ★ DWS 的 GMV 要能从 DWD 重算，ADS 要能对上 DWS
    断言层  5 项   ★ 把脏数据塞进去，你写的断言必须拦得住

## 分层可跑（重要）

任何一层的前置表不存在时，那一层**整层 SKIP 并说明缺什么**，而不是直接崩掉。
所以你可以写完 TODO 1 就跑一次，看到「结构层 4 项 PASS + 其余跳过」——
不必等六个 TODO 全部写完。`--layer N` 可以只跑某一层。

## 为什么「关联层」和「断言层」是重点

**关联层**：SCD2 最容易被写成「反正不改历史」的假实现 ——
  维表挂个 is_current 字段，关联时却用 `ON o.product_id = d.product_id`
  （忘掉时间条件）。那样 11 行维表会让 DWD 从 359 行膨胀到 400+ 行，且价格全错。
  所以这里必须抽查：9-04 之前的 P002 订单，unit_price 必须是 299。

**断言层**：数据质量断言最常见的失败不是「不会写」，是「写了但拦不住」。
  一个永远返回 passed=True 的 check 函数，看起来跑得很绿。
  所以这里会真的往表里塞脏数据，再调你的 `run_quality_checks` —— 它必须报错。
"""
import argparse
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

_ok = _fail = _skip = 0


def chk(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name:<42}{detail}")
    else:
        _fail += 1
        print(f"  FAIL  {name:<42}{detail}")
    return cond


def skip(name, why):
    global _skip
    _skip += 1
    print(f"  SKIP  {name:<42}（{why}）")


def head(t):
    print()
    print("=" * 88)
    print(t)
    print("=" * 88)


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def guard(tables, need, name, fn):
    """缺表 → SKIP；执行出错 → FAIL；否则用 fn() 返回的 (cond, detail) 判定。

    这样每项检查不必自己写 try/except，也不会因为一张表没建就把整份验收崩掉。
    """
    miss = [t for t in need if t not in tables]
    if miss:
        skip(name, "缺表 " + ",".join(miss))
        return
    try:
        cond, detail = fn()
        chk(name, cond, detail)
    except Exception as e:                                # noqa: BLE001
        chk(name, False, f"{type(e).__name__}: {str(e).splitlines()[0][:70]}")


# ============================================================================
# 第 1 层 · 结构
# ============================================================================
def layer1(con, tables, counts):
    head("1 · 结构层（表齐不齐、行数对不对）")

    need = ["ods_order", "ods_payment", "dim_product", "dwd_trade_order_detail",
            "dws_trade_daily", "ads_metric_daily_gmv", "ads_metric_pay_rate",
            "ads_metric_category_top"]
    miss = [t for t in need if t not in tables]
    chk("8 张表齐全", not miss, "" if not miss else f"缺 {miss}")

    guard(tables, ["ods_order"], "ods_order 行数 == CSV（无损落地）", lambda: (
        one(con, "SELECT COUNT(*) FROM ods_order") == counts["order"],
        f"{one(con,'SELECT COUNT(*) FROM ods_order')} vs {counts['order']}"))
    guard(tables, ["ods_payment"], "ods_payment 行数 == CSV（无损落地）", lambda: (
        one(con, "SELECT COUNT(*) FROM ods_payment") == counts["payment"],
        f"{one(con,'SELECT COUNT(*) FROM ods_payment')} vs {counts['payment']}"))
    guard(tables, ["dim_product"], "dim_product 行数 == 源表（不合并也不丢）", lambda: (
        one(con, "SELECT COUNT(*) FROM dim_product") == counts["dim"],
        f"{one(con,'SELECT COUNT(*) FROM dim_product')} vs {counts['dim']}"))
    guard(tables, ["dwd_trade_order_detail", "ods_order"],
          "DWD 行数 == ODS 行数（一单一行的宽表）", lambda: (
              one(con, "SELECT COUNT(*) FROM dwd_trade_order_detail") == counts["order"],
              f"{one(con,'SELECT COUNT(*) FROM dwd_trade_order_detail')} vs "
              f"{counts['order']}  ← 多出来说明维表关联没加时间条件，行被放大"))
    guard(tables, ["dws_trade_daily"], "dws_trade_daily 覆盖 7 天", lambda: (
        one(con, "SELECT COUNT(*) FROM dws_trade_daily") == 7,
        f"{one(con,'SELECT COUNT(*) FROM dws_trade_daily')} 天"))

    def dwd_cols():
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='dwd_trade_order_detail'").fetchall()}
        need_c = {"order_id", "user_id", "product_id", "product_name", "category",
                  "unit_price", "quantity", "amount", "order_status", "create_time",
                  "order_date", "pay_id", "pay_amount", "pay_status", "pay_time"}
        m = need_c - cols
        return (not m), ("" if not m else f"缺 {sorted(m)}")
    guard(tables, ["dwd_trade_order_detail"],
          "DWD 列齐全（含 unit_price / pay_status）", dwd_cols)


# ============================================================================
# 第 2 层 · SCD2
# ============================================================================
def layer2(con, tables):
    head("2 · SCD2 层（拉链区间与当前版本）")

    def types_ok():
        t = {r[0]: (r[1] or "").upper() for r in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='dim_product' "
            "AND column_name IN ('effective_from','effective_to')").fetchall()}
        ok = len(t) == 2 and all(v.startswith("TIMESTAMP") for v in t.values())
        return ok, str(t) + "  ← VARCHAR 与 create_time 比较时会抛 BinderException"
    guard(tables, ["dim_product"], "effective_from/to 都是 TIMESTAMP", types_ok)

    guard(tables, ["dim_product"], "每个商品恰好 1 条 is_current=1", lambda: (
        one(con, """SELECT COUNT(*) FROM (SELECT product_id FROM dim_product
                    GROUP BY product_id HAVING SUM(is_current) <> 1)""") == 0,
        f"异常商品 {one(con, '''SELECT COUNT(*) FROM (SELECT product_id FROM dim_product
              GROUP BY product_id HAVING SUM(is_current)<>1)''')} 个"))

    guard(tables, ["dim_product"], "拉链区间无重叠", lambda: (
        one(con, """SELECT COUNT(*) FROM dim_product a JOIN dim_product b
                    ON a.product_id=b.product_id AND a.effective_from < b.effective_from
                    WHERE a.effective_to > b.effective_from""") == 0,
        f"重叠对 {one(con, '''SELECT COUNT(*) FROM dim_product a JOIN dim_product b
              ON a.product_id=b.product_id AND a.effective_from < b.effective_from
              WHERE a.effective_to > b.effective_from''')}"))

    guard(tables, ["dim_product"], "拉链区间无空洞（首尾相接）", lambda: (
        one(con, """SELECT COUNT(*) FROM (SELECT product_id, effective_to,
                        LEAD(effective_from) OVER (PARTITION BY product_id
                                                   ORDER BY effective_from) AS nf
                    FROM dim_product)
                    WHERE nf IS NOT NULL AND effective_to <> nf""") == 0,
        f"断点 {one(con, '''SELECT COUNT(*) FROM (SELECT product_id, effective_to,
              LEAD(effective_from) OVER (PARTITION BY product_id ORDER BY effective_from) nf
              FROM dim_product) WHERE nf IS NOT NULL AND effective_to <> nf''')}"))

    def p002():
        rows = con.execute("""SELECT price, effective_from, effective_to, is_current
                              FROM dim_product WHERE product_id='P002'
                              ORDER BY effective_from""").fetchall()
        ok = (len(rows) == 2 and abs(rows[0][0] - 299.0) < 1e-6
              and abs(rows[1][0] - 269.0) < 1e-6)
        return ok, str([(r[0], str(r[1])[:10], str(r[2])[:10], r[3]) for r in rows])
    guard(tables, ["dim_product"], "P002 有 2 个版本且 299 → 269", p002)


# ============================================================================
# 第 3 层 · 关联（SCD2 到底有没有生效）
# ============================================================================
def layer3(con, tables):
    head("3 · 关联层（★ SCD2 到底有没有真的生效）")

    def p002_old():
        r = con.execute("""SELECT COUNT(*), MIN(unit_price), MAX(unit_price)
                           FROM dwd_trade_order_detail
                           WHERE product_id='P002'
                             AND create_time < TIMESTAMP '2026-09-04 00:00:00'""").fetchone()
        return (r[0] > 0 and r[1] == 299.0 and r[2] == 299.0), \
               f"{r[0]} 单，unit_price ∈ [{r[1]}, {r[2]}]"
    guard(tables, ["dwd_trade_order_detail"], "9-04 前的 P002 关联到旧价 299", p002_old)

    def p002_new():
        r = con.execute("""SELECT COUNT(*), MIN(unit_price), MAX(unit_price)
                           FROM dwd_trade_order_detail
                           WHERE product_id='P002'
                             AND create_time >= TIMESTAMP '2026-09-04 00:00:00'""").fetchone()
        return (r[0] > 0 and r[1] == 269.0 and r[2] == 269.0), \
               f"{r[0]} 单，unit_price ∈ [{r[1]}, {r[2]}]"
    guard(tables, ["dwd_trade_order_detail"], "9-04 后的 P002 关联到新价 269", p002_new)

    guard(tables, ["dwd_trade_order_detail"], "DWD 里 order_id 不重复（没被维表放大）", lambda: (
        one(con, """SELECT COUNT(*) FROM (SELECT order_id FROM dwd_trade_order_detail
                    GROUP BY order_id HAVING COUNT(*) > 1)""") == 0,
        f"重复 {one(con, '''SELECT COUNT(*) FROM (SELECT order_id FROM dwd_trade_order_detail
              GROUP BY order_id HAVING COUNT(*)>1)''')} 条"))

    guard(tables, ["dwd_trade_order_detail"], "DWD 的 category 全部关联上（无 NULL）", lambda: (
        one(con, "SELECT COUNT(*) FROM dwd_trade_order_detail WHERE category IS NULL") == 0,
        f"NULL {one(con,'SELECT COUNT(*) FROM dwd_trade_order_detail WHERE category IS NULL')} 条"))


# ============================================================================
# 第 4 层 · 对账
# ============================================================================
def layer4(con, tables):
    head("4 · 对账层（★ 下游能不能从上游重算出来）")

    def dws_vs_dwd():
        d = one(con, """
            WITH b AS (SELECT order_date,
                              ROUND(SUM(CASE WHEN pay_status='SUCCESS' THEN pay_amount
                                             ELSE 0 END), 2) AS gmv
                       FROM dwd_trade_order_detail GROUP BY order_date)
            SELECT COALESCE(MAX(ABS(a.gmv - b.gmv)), 0)
            FROM dws_trade_daily a JOIN b USING (order_date)""")
        return d < 0.01, f"最大差 {d}"
    guard(tables, ["dws_trade_daily", "dwd_trade_order_detail"],
          "DWS 的日 GMV == 从 DWD 重算（差 < 0.01）", dws_vs_dwd)

    def ads_gmv():
        d = one(con, """
            SELECT COALESCE(MAX(ABS(a.gmv - b.gmv)), 0)
            FROM ads_metric_daily_gmv a JOIN dws_trade_daily b USING (order_date)""")
        return d < 0.01, f"最大差 {d}"
    guard(tables, ["ads_metric_daily_gmv", "dws_trade_daily"], "ADS 日 GMV == DWS 日 GMV", ads_gmv)

    def ads_rate():
        d = one(con, """
            SELECT COALESCE(MAX(ABS(a.pay_rate - b.pay_rate)), 0)
            FROM ads_metric_pay_rate a JOIN dws_trade_daily b USING (order_date)""")
        return d < 1e-6, f"最大差 {d}"
    guard(tables, ["ads_metric_pay_rate", "dws_trade_daily"], "ADS 支付率 == DWS 支付率", ads_rate)

    def cat_total():
        a = one(con, "SELECT ROUND(SUM(gmv), 2) FROM ads_metric_category_top")
        b = one(con, """SELECT ROUND(SUM(pay_amount), 2) FROM dwd_trade_order_detail
                        WHERE pay_status='SUCCESS'""")
        return abs(a - b) < 0.01, f"{a} vs {b}"
    guard(tables, ["ads_metric_category_top", "dwd_trade_order_detail"],
          "类目 Top 求和 == DWD 成功支付总额", cat_total)

    def cat_rank():
        c = one(con, "SELECT COUNT(*) FROM ads_metric_category_top")
        mx = one(con, "SELECT MAX(rn) FROM ads_metric_category_top")
        mn = one(con, "SELECT MIN(rn) FROM ads_metric_category_top")
        return (c == mx and mn == 1), f"{c} 个类目，rn ∈ [{mn},{mx}]"
    guard(tables, ["ads_metric_category_top"], "类目名次从 1 连续排到 N", cat_rank)


# ============================================================================
# 第 5 层 · 断言（拦不拦得住）
# ============================================================================
def layer5(con, tables, mod):
    head("5 · 断言层（★ 你写的质量断言，能不能拦住脏数据）")

    labels = ["干净数据上断言全部通过（且 ≥5 条）", "注入重复 order_id",
              "注入负金额", "注入 GMV 突变", "注入空 user_id"]
    need = ["ods_order", "dws_trade_daily", "dwd_trade_order_detail"]
    miss = [t for t in need if t not in tables]
    if miss:
        for lb in labels:
            skip(lb, "缺表 " + ",".join(miss))
        return

    try:
        r = mod.run_quality_checks(con)
        ok = bool(r.get("passed")) and all(x.get("ok") for x in r.get("details", []))
        n = len(r.get("details", []))
        chk(labels[0], ok and n >= 5, f"{n} 条断言" + ("" if ok else "（有失败项）"))
    except Exception as e:                                # noqa: BLE001
        chk(labels[0], False, f"{type(e).__name__}: {str(e).splitlines()[0][:60]}")

    def inject(sql):
        """事务里注入脏数据 → 调断言 → 必须报错 → 回滚。

        注入本身被约束挡住（例如主键约束）也算「拦住了」—— 那是更早的一道防线。
        """
        con.execute("BEGIN")
        try:
            try:
                con.execute(sql)
            except Exception as e:                        # noqa: BLE001
                con.execute("ROLLBACK")
                return True, f"写入时就被约束挡住：{type(e).__name__}"
            res = mod.run_quality_checks(con)
            con.execute("ROLLBACK")
            bad = [d["name"] for d in res.get("details", []) if not d.get("ok")]
            return (not res.get("passed")), ("拦住了：" + ",".join(bad) if bad
                                             else "★ 没拦住（passed 仍为 True）")
        except Exception as e:                            # noqa: BLE001
            try:
                con.execute("ROLLBACK")
            except Exception:                             # noqa: BLE001
                pass
            return False, f"注入流程出错：{type(e).__name__}"

    cases = [
        (labels[1], """INSERT INTO ods_order
            SELECT order_id, user_id, product_id, quantity, amount,
                   order_status, create_time FROM ods_order LIMIT 1"""),
        (labels[2], """UPDATE ods_order SET amount = -1.0
            WHERE order_id = (SELECT MIN(order_id) FROM ods_order)"""),
        (labels[3], """UPDATE dws_trade_daily SET gmv = gmv * 3
            WHERE order_date = (SELECT MAX(order_date) FROM dws_trade_daily)"""),
        (labels[4], """UPDATE dwd_trade_order_detail SET user_id = NULL
            WHERE order_id = (SELECT MIN(order_id) FROM dwd_trade_order_detail)"""),
    ]
    for label, sql in cases:
        try:
            ok, detail = inject(sql)
        except Exception as e:                            # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}"
        chk(label, ok, detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="lab", choices=["lab", "ref"])
    ap.add_argument("--layer", type=int, default=0, help="1—5，只跑某一层")
    args = ap.parse_args()

    mod_name = "dwh_ref" if args.target == "ref" else "dwh_lab"
    try:
        mod = __import__(mod_name)
    except ImportError as e:
        print(f"x 导入 {mod_name} 失败：{e}")
        return 1
    for fn in ("build_ods", "build_dim", "build_dwd", "build_dws",
               "build_ads", "run_quality_checks"):
        if not hasattr(mod, fn):
            print(f"x {mod_name} 里找不到 {fn}()")
            return 1

    def count_rows(name):
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return sum(1 for _ in f) - 1
    counts = {"order": count_rows("ods_order.csv"),
              "payment": count_rows("ods_payment.csv"),
              "dim": count_rows("dim_product.csv")}

    db = os.path.join(HERE, f"_check_{args.target}.duckdb")
    if os.path.exists(db):
        os.remove(db)

    print("=" * 88)
    print(f"dwh_mini 验收 · target={args.target}"
          + (f" · 只跑第 {args.layer} 层" if args.layer else " · 全跑"))
    print("=" * 88)

    con = duckdb.connect(db)
    try:
        # 依次调用 build_*，任何一个失败都只记录、不中断 —— 这样才能看到已完成部分的进度
        for fn in ("build_ods", "build_dim", "build_dwd", "build_dws", "build_ads"):
            try:
                getattr(mod, fn)(con)
            except NotImplementedError:
                print(f"  （{fn} 还没实现）")
            except Exception as e:                        # noqa: BLE001
                print(f"  x {fn}() 出错：{type(e).__name__}: "
                      f"{str(e).splitlines()[0][:78]}")

        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        layers = {1: lambda: layer1(con, tables, counts),
                  2: lambda: layer2(con, tables),
                  3: lambda: layer3(con, tables),
                  4: lambda: layer4(con, tables),
                  5: lambda: layer5(con, tables, mod)}
        for i in ([args.layer] if args.layer else [1, 2, 3, 4, 5]):
            layers[i]()
    finally:
        con.close()
        for p in (db, db + ".wal"):
            if os.path.exists(p):
                os.remove(p)

    total = _ok + _fail + _skip
    print()
    print("=" * 88)
    print(f"{total} 项：{_ok} 通过 / {_fail} 失败 / {_skip} 跳过")
    print("=" * 88)
    if _fail:
        print("卡住看哪：")
        print("  · 结构层失败   → build_* 真的 CREATE TABLE 了吗；时间列转 TIMESTAMP 了吗")
        print("  · SCD2 层失败  → LEAD() 有没有 PARTITION BY product_id；effective_to 是不是被 COALESCE 转成了 VARCHAR")
        print("  · 关联层失败   → 维表 JOIN 少了 `AND o.create_time >= d.effective_from` 这类时间条件")
        print("  · 对账层失败   → GMV 口径不一致：两边是「下单日」还是「支付日」？必须同一个")
        print("  · 断言层失败   → check 函数可能永远返回 True，或阈值写反了")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
