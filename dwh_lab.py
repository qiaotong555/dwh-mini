"""dwh_mini · 把数仓分层从「集群」搬到「一个 duckdb 文件」。

    ..\\.venv\\Scripts\\python.exe dwh_lab.py            # 建仓 + 打印各层行数 + 跑断言
    ..\\.venv\\Scripts\\python.exe dwh_check.py          # 25 项验收（这才是判据）

## 这个项目要解决什么

你简历上那两个数仓项目跑不起来 —— 需要 Hadoop / Flink / Kafka 一整套集群。
面试官问「能跑吗」，你只能说「环境没了」。这句话会让两个项目一起打折。

这个项目把它重写成 **`pip install -r requirements.txt` 之后就能跑** 的版本：

    保留（这是你的能力证明）          简化（这是别人装不上的环境）
    ─────────────────────────         ──────────────────────────
    四层分层 ods_/dim_/dwd_/dws_       Kafka+Flume+Maxwell → 三份 CSV
    星型建模 + SCD2 拉链表             HBase 维表 → duckdb 表
    3 张核心事实表                     Flink 流处理 → 按天批处理
    5 条质量断言                       Oozie/Airflow → 一个 python 脚本

**判据不是「跑出数字」，是「断言能拦住脏数据」。**
大多数人做到「能出报表」就停了，那只是 ETL。数仓的活是「数据可信」。

## 数据（已经生成好，不用你造）

    data/ods_order.csv     359 单，跨 2026-09-01 ~ 09-07
    data/ods_payment.csv   317 条，只覆盖已支付订单（8% 退款）
    data/dim_product.csv   11 行 / 10 个商品 —— **P002 在 9-04 改过价（299 → 269）**

P002 那两个版本是整个项目唯一的考点：如果维表每个商品只有一行，
拉链表就是纯装饰，谁都写得出假实现。

## 六个 TODO

按顺序做，每个做完就能跑一次 `dwh_check.py` 看进度。**不要一次全写完再验。**
"""
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB = os.path.join(HERE, "dwh.duckdb")

# 维表的「永不过期」时间戳。用 '9999-12-31 23:59:59' 是行业惯例，
# 好处是 `create_time < effective_to` 这种比较天然成立，不必写 OR IS NULL。
FOREVER = "9999-12-31 23:59:59"


# ============================================================================
# TODO 1 · ODS 层：原样落地
# ============================================================================
def build_ods(con):
    """把三份 CSV 原样装进 duckdb，表名 ods_order / ods_payment。
    要求：
      · 列名与 CSV 表头完全一致（验收会点数）
      · 行数与 CSV 一致（359 / 317）—— **不要在这里去重、不要过滤状态**
      · create_time / pay_time 转成 TIMESTAMP（后面要按时间比较）
    提示：
      duckdb 可以直接 `CREATE OR REPLACE TABLE t AS SELECT * FROM read_csv_auto('路径')`
      时间列如果被推断成 VARCHAR，用 `CAST(x AS TIMESTAMP)` 显式转。
    ⚠ 常见错误：在这里就 `WHERE order_status='PAID'` 过滤掉未支付单。
       那是 DWD/DWS 的活。ODS 的职责是「原样落地」，过滤掉就再也找不回来了。
    """
    con.execute(f"""
            CREATE OR REPLACE TABLE ods_order AS
            SELECT 
                order_id,
                user_id,
                product_id,
                quantity,
                amount,
                order_status,
                CAST(create_time AS TIMESTAMP) AS create_time
            FROM read_csv_auto('{os.path.join(DATA, "ods_order.csv")}', header=true)
        """)
    con.execute(f"""
            CREATE OR REPLACE TABLE ods_payment AS
            SELECT 
                pay_id,
                order_id,
                pay_amount,
                pay_status,
                CAST(pay_time AS TIMESTAMP) AS pay_time
            FROM read_csv_auto('{os.path.join(DATA, "ods_payment.csv")}', header=true)
        """)


# ============================================================================
# TODO 2 · DIM 层：SCD2 拉链表（本项目唯一的难点）
# ============================================================================
def build_dim(con):
    """把 dim_product.csv（11 行，P002 有 2 版）变成拉链表 dim_product。
    目标表结构（列名要对）：
        product_id, product_name, category, price,
        effective_from, effective_to, is_current
    规则：
      · 同一个 product_id 按 update_time 排序，每条源记录的 effective_from = 它的 update_time
      · effective_to = **下一条**的 effective_from；最后一条 = FOREVER
      · is_current = 1 表示当前版本（每个商品恰好一条）
    提示：
      `LEAD(effective_from) OVER (PARTITION BY product_id ORDER BY effective_from)`
      这个窗口函数直接给你「下一条的起始时间」，两个字段一次算出来。
    ⚠ 常见错误：用 `MAX(update_time) GROUP BY product_id` 只留最新一版。
       那样历史价格就没了，9-04 之前的订单会全部关联到 269（错）。
    """
    con.execute(f"""
            CREATE OR REPLACE TABLE dim_product AS
            WITH sorted_src AS (
                SELECT 
                    product_id,
                    product_name,
                    category,
                    price,
                    CAST(update_time AS TIMESTAMP) AS effective_from
                FROM read_csv_auto('{os.path.join(DATA, "dim_product.csv")}', header=true)
            ),
            with_next AS (
                SELECT 
                    *,
                    LEAD(effective_from) OVER (
                        PARTITION BY product_id 
                        ORDER BY effective_from
                    ) AS next_effective_from
                FROM sorted_src
            )
            SELECT 
                product_id,
                product_name,
                category,
                price,
                effective_from,
                -- effective_to 必须保持 TIMESTAMP：给字符串字面量显式 CAST，
                -- 否则整列退化成 VARCHAR，DWD 的 o.create_time < d.effective_to 会抛 BinderException
                COALESCE(
                    next_effective_from,
                    CAST('{FOREVER}' AS TIMESTAMP)
                ) AS effective_to,
                CASE WHEN next_effective_from IS NULL THEN 1 ELSE 0 END AS is_current
            FROM with_next
        """)



# ============================================================================
# TODO 3 · DWD 层：事务事实表（把维表关联上去）
# ============================================================================
def build_dwd(con):
    """建 dwd_trade_order_detail：一单一行的宽表。

    列（验收会点数，缺一个就 FAIL）：

        order_id, user_id, product_id,
        product_name, category, unit_price,      ← 来自 dim_product
        quantity, amount, order_status, create_time,
        order_date,                              ← create_time 的日期部分
        pay_id, pay_amount, pay_status, pay_time ← 来自 ods_payment，可能是 NULL

    两个 JOIN：
      · dim_product：**必须带时间条件**，让订单关联到「下单当时」的维表版本
            ON o.product_id = d.product_id
           AND o.create_time >= d.effective_from
           AND o.create_time <  d.effective_to
      · ods_payment：LEFT JOIN（未支付/取消的订单没有支付记录，要保留）

    ⚠ 常见错误①：维表 JOIN 忘了时间条件。
       11 行维表会让 359 单膨胀到 400+ 行，而且价格全错。验收第 3 层就是查这个。
    ⚠ 常见错误②：用 INNER JOIN 关联支付。
       未支付的 42 单会整行消失，DWS 的「下单数」就偏小了。
    """
    con.execute("""
            CREATE OR REPLACE TABLE dwd_trade_order_detail AS
            SELECT 
                o.order_id,
                o.user_id,
                o.product_id,
                d.product_name,
                d.category,
                d.price AS unit_price,
                o.quantity,
                o.amount,
                o.order_status,
                o.create_time,
                CAST(o.create_time AS DATE) AS order_date,
                p.pay_id,
                p.pay_amount,
                p.pay_status,
                p.pay_time
            FROM ods_order o
            -- 关键：带时间条件的维表关联，确保取到下单当时的版本
            LEFT JOIN dim_product d
                ON o.product_id = d.product_id
               AND o.create_time >= d.effective_from
               AND o.create_time <  d.effective_to
            -- 关键：LEFT JOIN 支付表，保留未支付/取消的订单
            LEFT JOIN ods_payment p
                ON o.order_id = p.order_id
        """)


# ============================================================================
# TODO 4 · DWS 层：按天汇总
# ============================================================================
def build_dws(con):
    """建 dws_trade_daily：每天一行。

    列（口径必须和验收一致，验收会逐日对账）：

        order_date
        order_cnt    当天订单数（COUNT(*)，含未支付和取消）
        paid_cnt     order_status='PAID' 的单数
        refund_cnt   pay_status='REFUND' 的单数
        gmv          ★ pay_status='SUCCESS' 的 pay_amount 之和
        pay_rate     ★ 支付成功的订单数 / 当天订单数（0—1 的小数，不是百分比）

    ⚠ 常见错误：GMV 用 `SUM(amount)`（下单金额）而不是 `SUM(pay_amount)`。
       两者在有退款和未支付时会差几十个百分点。验收的对账层会抓出来。
    """
    con.execute("""
            CREATE OR REPLACE TABLE dws_trade_daily AS
            SELECT 
                order_date,
                COUNT(*) AS order_cnt,
                SUM(CASE WHEN order_status = 'PAID' THEN 1 ELSE 0 END) AS paid_cnt,
                SUM(CASE WHEN pay_status = 'REFUND' THEN 1 ELSE 0 END) AS refund_cnt,
                -- GMV 仅统计支付成功的实际支付金额
                SUM(CASE WHEN pay_status = 'SUCCESS' THEN pay_amount ELSE 0 END) AS gmv,
                -- 支付率 = 支付成功单数 / 总单数
                CAST(SUM(CASE WHEN pay_status = 'SUCCESS' THEN 1 ELSE 0 END) AS DOUBLE) 
                    / COUNT(*) AS pay_rate
            FROM dwd_trade_order_detail
            GROUP BY order_date
            ORDER BY order_date
        """)


# ============================================================================
# TODO 5 · ADS 层：三个指标表
# ============================================================================
def build_ads(con):
    """建三张指标表（都从 dws / dwd 出，不要再回到 ods —— 那是脱离分层）。

        ads_metric_daily_gmv      order_date, gmv
        ads_metric_pay_rate       order_date, pay_rate, pay_rate_pct（= pay_rate*100，保留 2 位）
        ads_metric_category_top   category, gmv, rn（rn 从 1 连续编号，按 gmv 降序）

    提示：
      类目 Top 的 gmv 来自 dwd 里 pay_status='SUCCESS' 的 pay_amount 按 category 汇总，
      所以它的总和必须等于 DWD 的成功支付总额（验收会对这个账）。

      `ROW_NUMBER() OVER (ORDER BY SUM(pay_amount) DESC)` 用窗口函数编名次。
    """
    # 1. 每日 GMV
    con.execute("""
           CREATE OR REPLACE TABLE ads_metric_daily_gmv AS
           SELECT order_date, gmv
           FROM dws_trade_daily
       """)

    # 2. 每日支付率
    con.execute("""
           CREATE OR REPLACE TABLE ads_metric_pay_rate AS
           SELECT 
               order_date, 
               pay_rate, 
               ROUND(pay_rate * 100, 2) AS pay_rate_pct
           FROM dws_trade_daily
       """)

    # 3. 类目 GMV Top 排行（从 DWD 聚合，保证与 DWS 总 GMV 对账一致）
    con.execute("""
           CREATE OR REPLACE TABLE ads_metric_category_top AS
           SELECT 
               category,
               SUM(pay_amount) AS gmv,
               ROW_NUMBER() OVER (ORDER BY SUM(pay_amount) DESC) AS rn
           FROM dwd_trade_order_detail
           WHERE pay_status = 'SUCCESS'
           GROUP BY category
       """)


# ============================================================================
# TODO 6 · 质量断言：5 条，能拦住脏数据
# ============================================================================
def run_quality_checks(con):
    """返回 {"passed": bool, "details": [{"name","ok","detail"}, ...]}，至少 5 条。

    五条要查什么（名字可以自己起，但语义必须覆盖）：

        ① ods_order 的 order_id 主键唯一          → 查出重复条数，应为 0
        ② amount >= 0 且 pay_amount >= 0         → 查出违例条数，应为 0
        ③ dws_trade_daily 的日 GMV 环比 < 30%     → 查出突变天数，应为 0
        ④ dwd 行数 == ods_order 行数              → 差值应 == 0
        ⑤ dwd 的 order_id/user_id/product_id 非空 → 查出空值条数，应为 0

    返回结构建议：
        {"passed": all(ok for ...),
         "details": [{"name": "ods_order 主键唯一", "ok": True, "detail": "violations=0"}, ...]}

    ⚠ 这一题最重要，也最容易写成假的。
      验收层会**真的往表里塞脏数据**（重复主键 / 负金额 / GMV 翻三倍 / 空 user_id），
      再调你这个函数 —— 它必须返回 passed=False。
      一个永远返回 True 的 check 函数，跑起来一片绿，但它什么也没验。
    """
    details = []

    # ① ods_order 主键唯一性
    dup_cnt = con.execute("SELECT COUNT(*) - COUNT(DISTINCT order_id) FROM ods_order").fetchone()[0]
    details.append({"name": "ods_order 主键唯一", "ok": dup_cnt == 0, "detail": f"violations={dup_cnt}"})

    # ② 金额非负校验
    #    必须查源层：ODS 才是「原样落地」的那一层，脏数据先进 ods_order / ods_payment；
    #    只查 DWD 会漏掉「源表被改脏但 DWD 未重算」的情况（验收就是这么注入的）。
    #    DWD 也查一遍，防的是下游被写脏。
    neg_cnt = con.execute("""
                          SELECT (SELECT COUNT(*) FROM ods_order WHERE amount < 0)
                               + (SELECT COUNT(*) FROM ods_payment WHERE pay_amount < 0)
                               + (SELECT COUNT(*) FROM dwd_trade_order_detail
                                  WHERE amount < 0 OR pay_amount < 0)
                          """).fetchone()[0]
    details.append({"name": "金额非负校验", "ok": neg_cnt == 0, "detail": f"violations={neg_cnt}"})

    # ③ DWS 日 GMV 环比波动 < 30%
    gmv_jump = con.execute("""
                           WITH daily AS (SELECT order_date,
                                                 gmv,
                                                 LAG(gmv) OVER (ORDER BY order_date) AS prev_gmv
                                          FROM dws_trade_daily)
                           SELECT COUNT(*)
                           FROM daily
                           WHERE prev_gmv > 0
                             AND ABS(gmv - prev_gmv) / prev_gmv >= 0.3
                           """).fetchone()[0]
    details.append({"name": "DWS GMV 环比波动<30%", "ok": gmv_jump == 0, "detail": f"violations={gmv_jump}"})

    # ④ DWD 行数 == ODS 订单行数
    dwd_cnt = con.execute("SELECT COUNT(*) FROM dwd_trade_order_detail").fetchone()[0]
    ods_cnt = con.execute("SELECT COUNT(*) FROM ods_order").fetchone()[0]
    diff = abs(dwd_cnt - ods_cnt)
    details.append({"name": "DWD与ODS行数一致", "ok": diff == 0, "detail": f"diff={diff}"})

    # ⑤ DWD 核心字段非空
    null_cnt = con.execute("""
                           SELECT COUNT(*)
                           FROM dwd_trade_order_detail
                           WHERE order_id IS NULL
                              OR user_id IS NULL
                              OR product_id IS NULL
                           """).fetchone()[0]
    details.append({"name": "DWD 核心字段非空", "ok": null_cnt == 0, "detail": f"violations={null_cnt}"})

    return {
        "passed": all(d["ok"] for d in details),
        "details": details
    }


# ============================================================================
# 以下不用改
# ============================================================================
def build_all(con):
    build_ods(con)
    build_dim(con)
    build_dwd(con)
    build_dws(con)
    build_ads(con)


def main():
    if "--rebuild" in sys.argv and os.path.exists(DB):
        os.remove(DB)
    con = duckdb.connect(DB)
    build_all(con)

    print("=" * 78)
    for t in ("ods_order", "ods_payment", "dim_product", "dwd_trade_order_detail",
              "dws_trade_daily", "ads_metric_daily_gmv", "ads_metric_pay_rate",
              "ads_metric_category_top"):
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:<28}{n:>6} 行")
        except Exception as e:                            # noqa: BLE001
            print(f"  {t:<28}   ✗ {type(e).__name__}: {e}")
    print("=" * 78)

    r = run_quality_checks(con)
    for d in r["details"]:
        print(f"  {'PASS' if d['ok'] else 'FAIL'}  {d['name']:<26}{d['detail']}")
    print(f"\n断言汇总：{'全部通过' if r['passed'] else '有失败项'}")
    print()
    print("DWS 明细：")
    try:
        for row in con.execute(
                "SELECT order_date, order_cnt, paid_cnt, gmv, pay_rate "
                "FROM dws_trade_daily ORDER BY order_date").fetchall():
            print(f"  {row[0]}  单 {row[1]:>3}  已付 {row[2]:>3}  "
                  f"GMV {row[3]:>10,.2f}  支付率 {row[4]:.2%}")
    except Exception as e:                                # noqa: BLE001
        print(f"  ✗ {e}")


if __name__ == "__main__":
    main()
